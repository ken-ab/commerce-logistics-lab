"""Separate, fixed validation of the lexical candidate policy chosen on development.

Reuses the frozen RRF arithmetic and label reader, never edits development evidence.
This is candidate retrieval validation, not an end-to-end business release test.
"""
from contextlib import closing
import argparse
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
import time

from research import candidate_retrieval_diagnostic as dev
from commerce_lab.catalog import Catalog

OUT = dev.ROOT / 'evidence/candidate_retrieval_validation_v1'
METHODS = ('fts_all', 'fts_any', 'rrf_all_any')


def register():
    if OUT.exists():
        raise FileExistsError('Validation registration already exists; inspect its state')
    original = dev.read(dev.OUT / 'registration.json')
    assert dev.sha(dev.CATALOG) == original['catalog_sha256']
    assert dev.sha(dev.QUERY_FILE) == dev.read(dev.ROOT / 'evidence/ranking_query_manifest.json')['sha256']
    selected = [r for r in dev.read(dev.QUERY_FILE)['queries'] if r['partition'] == 'validation' and r['locale'] == 'us']
    selected.sort(key=lambda r: (r['query_group_sha256'], r['query_id']))
    assert len(selected) == len({r['query_group_sha256'] for r in selected}) == 200
    assert not {r['query_group_sha256'] for r in selected} & {r['query_group_sha256'] for r in original['queries']}
    assert not {r['query_id'] for r in selected} & {r['query_id'] for r in original['queries']}
    texts = dev.query_texts(selected)  # IDs and text only, no relevance labels.
    paths = [Path(__file__), Path(dev.__file__), dev.ROOT / 'commerce_lab/catalog.py',
             dev.QUERY_FILE, dev.EXAMPLES, dev.ROOT / 'evidence/catalog_ingestion.json',
             dev.OUT / 'registration.json', dev.OUT / 'summary.json', dev.OUT / 'audit.json']
    value = {'registered_at': dev.now(), 'version': 'candidate-retrieval-validation-v1',
        'queries': [{**r, 'query': texts[r['query_id']]} for r in selected],
        'source_sha256': {p.relative_to(dev.ROOT).as_posix(): dev.sha(p) for p in paths},
        'catalog_sha256': original['catalog_sha256'], 'catalog_manifest': original['catalog_manifest'],
        'primary_comparison': 'Any minus All, chosen on the prior development diagnostic.',
        'secondary_comparison': 'RRF is descriptive only; no winner switching based on these results.',
        'methods': list(METHODS), 'depth': 100, 'rrf_k': 60,
        'execution_order': 'Query-group SHA order; alternating All/Any first; single sequential CPU pass.',
        'metrics': original['metrics'], 'label_policy': original['label_policy'],
        'candidate_gate': {'bootstrap_lower_bound_gt': 0, 'known_exact_hit_queries_no_worse': True,
                           'any_mean_latency_at_most_seconds': 0.75, 'any_p95_at_most_seconds': 2.0},
        'gate_scope': 'Candidate-stage evidence to continue engineering; not authority to replace the default. '
            'End-to-end relevance/constraint checks remain necessary. Latency is a one-pass feasibility screen, not SLA.',
        'bootstrap': {'resamples': 2000, 'seed': 20260910, 'unit': 'paired query group'},
        'partition_scope': 'Existing original validation groups, disjoint from this candidate development group set; '
            'previous project experiments may have used these groups. Not a newly collected unseen benchmark.',
        'constraints': ['No generated queries, embedding calls or neural reranking.',
            'No deployment, model-selection, apparel or prior evidence changes.',
            'Unknown judgments remain unknown. No full-catalogue Precision, NDCG or business success claim.'],
        'preflight': dev.preflight(), 'ledger_before': dev.ledger(), 'new_paid_calls': 0, 'new_gpu_forwards': 0}
    OUT.mkdir()
    (OUT / 'runs').mkdir()
    dev.write(OUT / 'registration.json', value)
    print(json.dumps({'registered': True, 'queries': 200, 'disjoint_candidate_development': True, 'new_paid_calls': 0}))


def verified():
    registration = dev.read(OUT / 'registration.json')
    assert all(dev.sha(dev.ROOT / p) == digest for p, digest in registration['source_sha256'].items())
    assert dev.sha(dev.CATALOG) == registration['catalog_sha256']
    return registration


def run():
    registration = verified()
    if (OUT / 'retrieval_complete.json').exists():
        raise FileExistsError('Completed validation retrieval is frozen')
    catalog = Catalog(dev.CATALOG)
    start = time.monotonic()
    registration_sha = dev.sha(OUT / 'registration.json')
    for index, query in enumerate(registration['queries']):
        path = OUT / 'runs' / f"{query['query_id']}.json"
        if path.exists():
            old = dev.read(path)
            assert old['query'] == query and old['registration_sha256'] == registration_sha
            continue
        lists, durations = {}, {}
        for mode in (('all', 'any') if index % 2 == 0 else ('any', 'all')):
            before = time.monotonic()
            rows = catalog.search(query['query'], locale='us', limit=100, match_mode=mode)
            durations[mode] = time.monotonic() - before
            lists[mode] = [r['id'] for r in rows]
            assert len(rows) == len(set(lists[mode])) <= 100 and all(r['locale'] == 'us' for r in rows)
        before = time.monotonic()
        fused = dev.fuse([lists['all'], lists['any']])
        fusion_seconds = time.monotonic() - before
        dev.write(path, {'recorded_at': dev.now(), 'registration_sha256': registration_sha, 'query': query,
            'all': lists['all'], 'any': lists['any'], 'rrf': fused, 'durations': durations, 'fusion_seconds': fusion_seconds})
        if (index + 1) % 20 == 0:
            print(json.dumps({'queries_done': index + 1, 'total': 200, 'elapsed_seconds': round(time.monotonic() - start, 2)}), flush=True)
    paths = sorted((OUT / 'runs').glob('*.json'))
    assert len(paths) == 200
    assert dev.sha(dev.CATALOG) == registration['catalog_sha256']
    dev.write(OUT / 'retrieval_complete.json', {'completed_at': dev.now(), 'run_files': 200,
        'result_sha256': {p.relative_to(OUT).as_posix(): dev.sha(p) for p in paths},
        'ledger_after': dev.ledger(), 'catalog_unchanged': True, 'new_paid_calls': 0, 'new_gpu_forwards': 0})


def score():
    if (OUT / 'summary.json').exists():
        raise FileExistsError('Completed validation scores are frozen')
    registration = verified()
    complete = dev.read(OUT / 'retrieval_complete.json')
    assert all(dev.sha(OUT / p) == digest for p, digest in complete['result_sha256'].items())
    judgments = dev.labels(registration['queries'])  # Label read only after retrieval completed.
    rows = []
    for query in registration['queries']:
        raw = dev.read(OUT / 'runs' / f"{query['query_id']}.json")
        known = judgments[query['query_id']]
        exact = {k for k, label in known.items() if label == 'E'}
        methods = {'fts_all': raw['all'], 'fts_any': raw['any'], 'rrf_all_any': [r['id'] for r in raw['rrf'][:100]]}
        metrics = {}
        for method, ids in methods.items():
            seconds = raw['durations']['all'] if method == 'fts_all' else raw['durations']['any'] if method == 'fts_any' else sum(raw['durations'].values()) + raw['fusion_seconds']
            metrics[method] = {'known_exact_coverage@20': len(set(ids[:20]) & exact) / len(exact) if exact else None,
                'known_exact_coverage@100': len(set(ids) & exact) / len(exact) if exact else None,
                'any_known_exact@100': bool(set(ids) & exact), 'candidate_count': len(ids), 'empty': not ids,
                'known_judged_fraction@100': len(set(ids) & set(known)) / len(ids) if ids else None,
                'retrieval_wall_seconds': seconds}
        rows.append({'query': query, 'known_labels': known, 'known_exact_count': len(exact), 'metrics': metrics})
    aggregates = {}
    for method in METHODS:
        values = [r['metrics'][method] for r in rows]
        aggregates[method] = {key: statistics.mean(v[key] for v in values if v[key] is not None) for key in
            ['known_exact_coverage@20', 'known_exact_coverage@100', 'known_judged_fraction@100', 'candidate_count', 'retrieval_wall_seconds']}
        aggregates[method].update(empty_result_count=sum(v['empty'] for v in values),
            queries_with_known_exact_hit=sum(v['any_known_exact@100'] for v in values),
            retrieval_latency_p95_seconds=sorted(v['retrieval_wall_seconds'] for v in values)[math.ceil(.95 * len(values)) - 1])
    deltas = [r['metrics']['fts_any']['known_exact_coverage@100'] - r['metrics']['fts_all']['known_exact_coverage@100']
              for r in rows if r['known_exact_count']]
    rng = random.Random(registration['bootstrap']['seed'])
    boot = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
    bounds = [boot[49], boot[1949]]
    baseline, candidate = aggregates['fts_all'], aggregates['fts_any']
    gate = {'coverage': bounds[0] > 0, 'query_hits': candidate['queries_with_known_exact_hit'] >= baseline['queries_with_known_exact_hit'],
        'mean_latency': candidate['retrieval_wall_seconds'] <= .75,
        'p95_latency': candidate['retrieval_latency_p95_seconds'] <= 2}
    with closing(sqlite3.connect(dev.CATALOG.as_uri() + '?mode=ro', uri=True)) as db:
        positives = {k for r in rows for k, label in r['known_labels'].items() if label == 'E'}
        assert all(db.execute("SELECT 1 FROM products WHERE id=? AND locale='us'", (k,)).fetchone() for k in positives)
    result = {'completed_at': dev.now(), 'registration_sha256': dev.sha(OUT / 'registration.json'),
        'retrieval_complete_sha256': dev.sha(OUT / 'retrieval_complete.json'), 'queries': 200,
        'queries_with_known_exact': len(deltas), 'known_exact_query_product_pairs': sum(r['known_exact_count'] for r in rows),
        'all_known_exact_ids_in_us_catalog': True, 'methods': aggregates,
        'primary_any_minus_all_at100': {'mean': statistics.mean(deltas), 'query_bootstrap_percentile_95': bounds,
            'positive': sum(v > 0 for v in deltas), 'equal': sum(v == 0 for v in deltas), 'negative': sum(v < 0 for v in deltas)},
        'candidate_gate': gate, 'candidate_gate_passed': all(gate.values()), 'gate_scope': registration['gate_scope'],
        'ledger_after': dev.ledger(), 'new_paid_calls': 0, 'new_gpu_forwards': 0, 'new_business_runs': 0,
        'deployment_changed': False, 'partition_scope': registration['partition_scope'], 'limits': registration['constraints']}
    dev.write(OUT / 'case_metrics.json', rows)
    dev.write(OUT / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['register', 'run', 'score'])
    phase = parser.parse_args().phase
    {'register': register, 'run': run, 'score': score}[phase]()

"""Registered, CPU-only full-catalogue candidate diagnostic on known dev queries.

No generation, embeddings, reranker service, API calls or deployment changes.
This compares lexical matching policies and their rank fusion, not LLM rewrites.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
import time

from commerce_lab.catalog import Catalog


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/candidate_retrieval_v1'
QUERY_FILE = ROOT / 'data/ranking_queries_v1.json'
EXAMPLES = ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
CATALOG = ROOT / 'data/catalog.sqlite'
K = 60
DEPTH = 100
METHODS = ('fts_all', 'fts_any', 'rrf_all_any')


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8') as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def ledger():
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        cost, rows = db.execute('SELECT SUM(COALESCE(charged,reserved)), COUNT(*) FROM calls').fetchone()
    return {'micro_cny': cost, 'rows': rows}


def fuse(rankings, *, k=K):
    """One-based RRF, stable identity and tie breaking; one vote per list/ID."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError('k must be a nonnegative integer')
    votes = {}
    for ranking in rankings:
        if len(ranking) != len(set(ranking)):
            raise ValueError('Duplicate document identity in one ranking')
        for rank, ident in enumerate(ranking, 1):
            votes.setdefault(ident, []).append(1 / (k + rank))
    scores = {ident: math.fsum(parts) for ident, parts in votes.items()}
    return [{'id': ident, 'rrf_score': scores[ident]} for ident in sorted(scores, key=lambda d: (-scores[d], d))]


def preflight():
    # Independent hand-calculated ranks: B has ranks 2 and 1; A and C one vote.
    result = fuse([['A', 'B'], ['B', 'C']])
    assert [r['id'] for r in result] == ['B', 'A', 'C']
    assert math.isclose(result[0]['rrf_score'], 1 / 62 + 1 / 61, rel_tol=1e-14)
    assert math.isclose(result[1]['rrf_score'], 1 / 61, rel_tol=1e-14)
    assert fuse([['A', 'B'], ['B', 'C']]) == fuse([['B', 'C'], ['A', 'B']])
    assert [r['id'] for r in fuse([['B'], ['A']])] == ['A', 'B']
    assert fuse([[], []]) == []
    try:
        fuse([['A', 'A']])
    except ValueError:
        duplicate_rejected = True
    else:
        duplicate_rejected = False
    assert duplicate_rejected
    return {'hand_calculated_ranks': True, 'one_based_denominator': True,
            'list_order_invariance': True, 'stable_ties': True, 'empty_union': True,
            'duplicate_identity_rejected': True}


def query_texts(selection):
    import duckdb
    with closing(duckdb.connect()) as db:
        db.execute('SET threads=2')
        db.execute('CREATE TEMP TABLE selected(query_id BIGINT)')
        db.executemany('INSERT INTO selected VALUES (?)', [(q['query_id'],) for q in selection])
        rows = db.execute('''SELECT DISTINCT e.query_id,e.query FROM read_parquet(?) e
            JOIN selected s USING(query_id) WHERE e.product_locale='us' AND e.small_version=1
            ORDER BY e.query_id''', [str(EXAMPLES)]).fetchall()
    assert len(rows) == len(selection) and len({q for q, _ in rows}) == len(selection)
    return dict(rows)


def register():
    if OUT.exists():
        raise FileExistsError('Registration/evidence exists; inspect before resuming')
    manifest = read(ROOT / 'evidence/ranking_query_manifest.json')
    assert sha(QUERY_FILE) == manifest['sha256']
    selection = [q for q in read(QUERY_FILE)['queries'] if q['partition'] == 'development' and q['locale'] == 'us']
    selection.sort(key=lambda q: (q['query_group_sha256'], q['query_id']))
    assert len(selection) == len({q['query_group_sha256'] for q in selection}) == 200
    checks = preflight()
    texts = query_texts(selection)  # Reads only IDs/text, not relevance labels.
    source_paths = [Path(__file__), ROOT / 'commerce_lab/catalog.py', ROOT / 'commerce_lab/retrieval.py',
                    QUERY_FILE, EXAMPLES, ROOT / 'evidence/catalog_ingestion.json']
    with closing(sqlite3.connect(CATALOG.as_uri() + '?mode=ro', uri=True)) as db:
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
    assert metadata['complete'] == 'true'
    registration = {'registered_at': now(), 'version': 'candidate-retrieval-dev-v1',
        'queries': [{**q, 'query': texts[q['query_id']]} for q in selection],
        'source_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in source_paths},
        'catalog_sha256': sha(CATALOG), 'catalog_bytes': CATALOG.stat().st_size,
        'catalog_manifest': json.loads(metadata['manifest']), 'ledger_at_registration': ledger(),
        'preflight': checks, 'methods': list(METHODS), 'candidate_depth_each_list': DEPTH, 'rrf_k': K,
        'ranking_policy': 'Original query; existing Catalog all/any policies; equal-weight one-based RRF; ID ties.',
        'execution_order': 'Fixed query-group SHA order; alternate all/any first by query index; sequential, one pass.',
        'metrics': ['known_exact_coverage@20', 'known_exact_coverage@100', 'any_known_exact@100',
                    'known_judged_fraction@100', 'candidate_count', 'empty_result_count', 'retrieval_wall_seconds'],
        'label_policy': 'Task 1 small_version=1, us, exact E as relevant. E/S/C/I count as judged; '
                        'unjudged documents remain unknown. Queries with zero known E excluded only from exact-coverage macro means.',
        'comparison': '200 already-used development query groups; not held-out or production recall. '
                      'Union up to 200 is reported only as an unequal-budget diagnostic ceiling.',
        'latency_scope': 'One local CPU pass; warm/cache/order effects remain. RRF latency = both reads + fusion, not one read.',
        'decision': 'Diagnostic only; no automatic deployment or changes to frozen ranking/business experiments.',
        'new_paid_calls': 0, 'new_gpu_forwards': 0, 'new_business_runs': 0}
    OUT.mkdir(parents=True)
    (OUT / 'runs').mkdir()
    write(OUT / 'registration.json', registration)
    print(json.dumps({'registered': True, 'queries': 200, 'catalog_products': registration['catalog_manifest']['products'],
                      'new_paid_calls': 0, 'preflight': checks}), flush=True)


def verify_registration():
    registration = read(OUT / 'registration.json')
    for name, expected in registration['source_sha256'].items():
        assert sha(ROOT / name) == expected, 'Registered source changed: ' + name
    assert sha(CATALOG) == registration['catalog_sha256'], 'Registered catalogue changed'
    return registration


def run():
    registration = verify_registration()
    if (OUT / 'retrieval_complete.json').exists():
        raise FileExistsError('Completed retrieval is frozen; do not run again')
    catalog = Catalog(CATALOG)
    started = time.monotonic()
    for index, query in enumerate(registration['queries']):
        path = OUT / 'runs' / (str(query['query_id']) + '.json')
        if path.exists():
            old = read(path)
            assert old['query'] == query and old['registration_sha256'] == sha(OUT / 'registration.json')
            continue
        lists, durations = {}, {}
        for mode in (('all', 'any') if index % 2 == 0 else ('any', 'all')):
            before = time.monotonic()
            rows = catalog.search(query['query'], locale='us', limit=DEPTH, match_mode=mode)
            durations[mode] = time.monotonic() - before
            assert all(row['locale'] == 'us' for row in rows)
            ids = [row['id'] for row in rows]
            assert len(ids) == len(set(ids)) <= DEPTH
            lists[mode] = ids
        before = time.monotonic()
        fused = fuse([lists['all'], lists['any']])
        fusion_seconds = time.monotonic() - before
        assert {r['id'] for r in fused} == set(lists['all']) | set(lists['any'])
        write(path, {'recorded_at': now(), 'query': query, 'registration_sha256': sha(OUT / 'registration.json'),
            'all': lists['all'], 'any': lists['any'], 'rrf': fused, 'durations': durations,
            'fusion_seconds': fusion_seconds, 'new_paid_calls': 0})
        if (index + 1) % 10 == 0:
            print(json.dumps({'queries_done': index + 1, 'total': 200,
                              'elapsed_seconds': round(time.monotonic() - started, 2)}), flush=True)
    paths = sorted((OUT / 'runs').glob('*.json'))
    assert len(paths) == len(registration['queries']) == 200
    assert sha(CATALOG) == registration['catalog_sha256']
    write(OUT / 'retrieval_complete.json', {'completed_at': now(), 'run_files': 200, 'catalog_unchanged': True,
        'result_sha256': {p.relative_to(OUT).as_posix(): sha(p) for p in paths},
        'source_sha256': registration['source_sha256'], 'new_paid_calls': 0, 'new_gpu_forwards': 0,
        'ledger_after': ledger()})


def labels(selection):
    import duckdb
    with closing(duckdb.connect()) as db:
        db.execute('SET threads=2')
        db.execute('CREATE TEMP TABLE selected(query_id BIGINT)')
        db.executemany('INSERT INTO selected VALUES (?)', [(q['query_id'],) for q in selection])
        rows = db.execute('''SELECT e.query_id,e.product_id,e.esci_label FROM read_parquet(?) e
            JOIN selected s USING(query_id) WHERE e.product_locale='us' AND e.small_version=1''', [str(EXAMPLES)]).fetchall()
    out = {q['query_id']: {} for q in selection}
    for qid, product, label in rows:
        ident = 'us:' + product
        assert label in {'E', 'S', 'C', 'I'}
        assert ident not in out[qid] or out[qid][ident] == label
        out[qid][ident] = label
    return out


def score():
    if (OUT / 'summary.json').exists():
        raise FileExistsError('Completed diagnostic is frozen')
    registration = verify_registration()
    complete = read(OUT / 'retrieval_complete.json')
    assert all(sha(OUT / p) == h for p, h in complete['result_sha256'].items())
    judgments = labels(registration['queries'])  # First relevance-label read by this diagnostic, after retrieval.
    rows = []
    for q in registration['queries']:
        result = read(OUT / 'runs' / (str(q['query_id']) + '.json'))
        known = judgments[q['query_id']]
        exact = {ident for ident, label in known.items() if label == 'E'}
        rankings = {'fts_all': result['all'], 'fts_any': result['any'],
                    'rrf_all_any': [r['id'] for r in result['rrf'][:DEPTH]]}
        metrics = {}
        for method, ranking in rankings.items():
            metrics[method] = {
                'known_exact_coverage@20': len(set(ranking[:20]) & exact) / len(exact) if exact else None,
                'known_exact_coverage@100': len(set(ranking) & exact) / len(exact) if exact else None,
                'any_known_exact@100': bool(set(ranking) & exact),
                'known_judged_fraction@100': len(set(ranking) & set(known)) / len(ranking) if ranking else None,
                'candidate_count': len(ranking), 'empty': not ranking,
                'retrieval_wall_seconds': (result['durations']['all'] if method == 'fts_all' else
                    result['durations']['any'] if method == 'fts_any' else
                    sum(result['durations'].values()) + result['fusion_seconds']),
            }
        union = set(result['all']) | set(result['any'])
        rows.append({'query': q, 'known_label_counts': dict(Counter(known.values())), 'known_labels': known,
            'metrics': metrics, 'union_size': len(union),
            'union_known_exact_coverage': len(union & exact) / len(exact) if exact else None})
    summary = {}
    for method in METHODS:
        measures = [r['metrics'][method] for r in rows]
        summary[method] = {key: statistics.mean(m[key] for m in measures if m[key] is not None)
            for key in ('known_exact_coverage@20', 'known_exact_coverage@100', 'known_judged_fraction@100',
                        'candidate_count', 'retrieval_wall_seconds')}
        summary[method].update(empty_result_count=sum(m['empty'] for m in measures),
            queries_with_known_exact_hit=sum(m['any_known_exact@100'] for m in measures),
            retrieval_latency_p95_seconds=sorted(m['retrieval_wall_seconds'] for m in measures)[math.ceil(.95 * len(measures)) - 1])
    deltas = [r['metrics']['rrf_all_any']['known_exact_coverage@100'] - r['metrics']['fts_all']['known_exact_coverage@100']
              for r in rows if r['metrics']['fts_all']['known_exact_coverage@100'] is not None]
    rng = random.Random(20260909)
    boot = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
    result = {'completed_at': now(), 'registration_sha256': sha(OUT / 'registration.json'),
        'retrieval_complete_sha256': sha(OUT / 'retrieval_complete.json'), 'queries': len(rows),
        'queries_with_known_exact': len(deltas), 'zero_known_exact_queries': len(rows) - len(deltas),
        'known_label_counts': dict(sum((Counter(r['known_label_counts']) for r in rows), Counter())),
        'methods': summary, 'union_mean_size': statistics.mean(r['union_size'] for r in rows),
        'union_known_exact_coverage': statistics.mean(r['union_known_exact_coverage'] for r in rows if r['union_known_exact_coverage'] is not None),
        'rrf_minus_all_at100': {'mean': statistics.mean(deltas), 'query_bootstrap_percentile_95': [boot[49], boot[1949]],
            'positive': sum(d > 0 for d in deltas), 'equal': sum(d == 0 for d in deltas), 'negative': sum(d < 0 for d in deltas),
            'resamples': 2000, 'seed': 20260909, 'scope': 'Development-query resampling only; not generalization evidence.'},
        'ledger_after': ledger(), 'new_paid_calls': 0, 'new_gpu_forwards': 0, 'new_business_runs': 0,
        'unchanged_catalog': sha(CATALOG) == registration['catalog_sha256'], 'deployment_changed': False,
        'limits': [registration['comparison'], registration['label_policy'], registration['latency_scope'],
                  'No LLM rewriting, HyDE, dense retrieval or answer generation was implemented or evaluated.',
                  'No NDCG/precision claim over unjudged full-catalogue candidates; no score or method changes in prior frozen runs.']}
    write(OUT / 'case_metrics.json', rows)
    write(OUT / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('register', 'run', 'score'))
    args = parser.parse_args()
    {'register': register, 'run': run, 'score': score}[args.phase]()

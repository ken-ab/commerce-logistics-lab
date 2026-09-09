"""Audit frozen validation candidates and report every Any-versus-All regression.

No catalog search, model call, policy tuning or deployment. Judgments are checked
against the registered public Parquet, independently of the scoring helper.
"""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/candidate_retrieval_validation_v1'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def close(left, right):
    assert math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-14), (left, right)


def main():
    import duckdb

    target = OUT / 'audit.json'
    if target.exists():
        raise FileExistsError('Validation audit already exists; preserve completed evidence')
    registration = read(OUT / 'registration.json')
    complete = read(OUT / 'retrieval_complete.json')
    summary = read(OUT / 'summary.json')
    cases = read(OUT / 'case_metrics.json')
    reg_sha = sha(OUT / 'registration.json')
    assert reg_sha == summary['registration_sha256']
    assert sha(OUT / 'retrieval_complete.json') == summary['retrieval_complete_sha256']
    assert all(sha(ROOT / name) == digest for name, digest in registration['source_sha256'].items())
    assert all(sha(OUT / name) == digest for name, digest in complete['result_sha256'].items())
    assert len(complete['result_sha256']) == len(cases) == 200
    assert [r['query'] for r in cases] == registration['queries']
    qids = [r['query']['query_id'] for r in cases]
    assert len(set(qids)) == 200
    dev = read(ROOT / 'evidence/candidate_retrieval_v1/registration.json')['queries']
    assert not set(qids) & {q['query_id'] for q in dev}
    assert not {r['query']['query_group_sha256'] for r in cases} & {q['query_group_sha256'] for q in dev}

    parquet = ROOT / 'upstream/esci-data/shopping_queries_dataset/shopping_queries_dataset_examples.parquet'
    original = defaultdict(dict)
    with closing(duckdb.connect()) as db:
        db.execute('SET threads=2')
        db.execute('CREATE TEMP TABLE selected(query_id BIGINT)')
        db.executemany('INSERT INTO selected VALUES (?)', [(qid,) for qid in qids])
        rows = db.execute('''SELECT e.query_id,e.product_id,e.esci_label
            FROM read_parquet(?) e JOIN selected s USING(query_id)
            WHERE e.product_locale='us' AND e.small_version=1''', [str(parquet)]).fetchall()
        for qid, product_id, label in rows:
            ident = 'us:' + product_id
            assert original[qid].get(ident, label) == label
            original[qid][ident] = label

    checked = 0
    exact_ids, all_candidates = set(), set()
    hits = Counter()
    regressions, empty_any, deltas = [], [], []
    by_method = defaultdict(list)
    for row in cases:
        qid = row['query']['query_id']
        raw = read(OUT / 'runs' / f'{qid}.json')
        assert raw['query'] == row['query'] and raw['registration_sha256'] == reg_sha
        assert row['known_labels'] == original[qid]
        exact = {ident for ident, label in original[qid].items() if label == 'E'}
        assert len(exact) == row['known_exact_count'] > 0
        exact_ids.update(exact)
        votes = defaultdict(list)
        for ranking in (raw['all'], raw['any']):
            for rank, ident in enumerate(ranking, 1):
                votes[ident].append(1 / (60 + rank))
        scores = {ident: math.fsum(values) for ident, values in votes.items()}
        expected_rrf = sorted(scores, key=lambda ident: (-scores[ident], ident))
        assert [r['id'] for r in raw['rrf']] == expected_rrf
        for item in raw['rrf']:
            close(item['rrf_score'], scores[item['id']])
        rankings = {'fts_all': raw['all'], 'fts_any': raw['any'], 'rrf_all_any': expected_rrf[:100]}
        for method, ranking in rankings.items():
            assert len(ranking) == len(set(ranking)) <= 100
            all_candidates.update(ranking)
            selected = set(ranking)
            metric = row['metrics'][method]
            close(len(selected & exact) / len(exact), metric['known_exact_coverage@100'])
            close(len(set(ranking[:20]) & exact) / len(exact), metric['known_exact_coverage@20'])
            assert metric['empty'] == (not ranking)
            assert metric['any_known_exact@100'] == bool(selected & exact)
            assert metric['candidate_count'] == len(ranking)
            if ranking:
                close(len(selected & set(original[qid])) / len(ranking), metric['known_judged_fraction@100'])
            else:
                assert metric['known_judged_fraction@100'] is None
            seconds = raw['durations']['all'] if method == 'fts_all' else raw['durations']['any'] if method == 'fts_any' else sum(raw['durations'].values()) + raw['fusion_seconds']
            assert math.isfinite(seconds) and seconds >= 0
            close(metric['retrieval_wall_seconds'], seconds)
            hits[method] += len(selected & exact)
            by_method[method].append(metric)
            checked += 1
        left, right = exact & set(raw['all']), exact & set(raw['any'])
        delta = (len(right) - len(left)) / len(exact)
        deltas.append(delta)
        if delta < 0:
            regressions.append({'query': row['query'], 'known_exact': len(exact),
                'all_hits': len(left), 'any_hits': len(right), 'net_coverage_change': delta,
                'all_candidates': len(raw['all']), 'any_candidates': len(raw['any']),
                'dropped_known_exact': [{'id': ident, 'all_rank': raw['all'].index(ident) + 1}
                                        for ident in sorted(left - right)],
                'gained_known_exact': [{'id': ident, 'any_rank': raw['any'].index(ident) + 1}
                                       for ident in sorted(right - left)]})
        if not raw['any']:
            empty_any.append(row['query'])

    for method, metrics in by_method.items():
        for key in ('known_exact_coverage@20', 'known_exact_coverage@100', 'known_judged_fraction@100',
                    'candidate_count', 'retrieval_wall_seconds'):
            close(statistics.mean(m[key] for m in metrics if m[key] is not None), summary['methods'][method][key])
        assert sum(m['empty'] for m in metrics) == summary['methods'][method]['empty_result_count']
        assert sum(m['any_known_exact@100'] for m in metrics) == summary['methods'][method]['queries_with_known_exact_hit']
        close(sorted(m['retrieval_wall_seconds'] for m in metrics)[189], summary['methods'][method]['retrieval_latency_p95_seconds'])
    delta_report = summary['primary_any_minus_all_at100']
    close(statistics.mean(deltas), delta_report['mean'])
    assert [sum(d > 0 for d in deltas), sum(d == 0 for d in deltas), sum(d < 0 for d in deltas)] == [delta_report[k] for k in ('positive', 'equal', 'negative')]
    rng = random.Random(registration['bootstrap']['seed'])
    boot = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(registration['bootstrap']['resamples']))
    for a, b in zip([boot[49], boot[1949]], delta_report['query_bootstrap_percentile_95']):
        close(a, b)

    # ID eligibility only; this does not issue another retrieval query.
    eligible = set()
    with closing(sqlite3.connect((ROOT / 'data/catalog.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        ids = sorted(exact_ids | all_candidates)
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            eligible.update(r[0] for r in db.execute("SELECT id FROM products WHERE locale='us' AND id IN ("
                + ','.join('?' for _ in batch) + ')', batch))
    assert eligible == exact_ids | all_candidates
    total_exact = sum(r['known_exact_count'] for r in cases)
    assert total_exact == summary['known_exact_query_product_pairs'] == 1577
    files = ['registration.json', 'retrieval_complete.json', 'case_metrics.json', 'summary.json']
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
        'scope': 'Post-hoc audit of frozen validation candidates; not another independent experiment or parameter tuning.',
        'script_sha256': sha(Path(__file__)), 'evidence_sha256': {p: sha(OUT / p) for p in files},
        'verified_run_files': 200, 'verified_query_method_pairs': checked,
        'original_parquet_judgments_verified': True, 'disjoint_candidate_development_queries': True,
        'known_exact_query_product_pairs': total_exact, 'unique_known_exact_ids': len(exact_ids),
        'all_candidate_and_positive_ids_in_us_catalog': True,
        'micro_known_exact_hits_at100': dict(hits), 'bootstrap_recomputed': True,
        'all_any_regressions_vs_all': regressions, 'all_any_empty_queries': empty_any,
        'regression_scope': 'Known E IDs absent from the Any top 100 are listed. These ranks alone do not establish why an unknown candidate is irrelevant or a causal semantic failure.',
        'new_paid_calls': 0, 'new_retrieval_calls': 0, 'new_gpu_forwards': 0, 'deployment_changed': False}
    with target.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('all_any_regressions_vs_all',)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

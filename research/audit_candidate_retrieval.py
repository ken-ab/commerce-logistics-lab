"""Read-only, post-hoc audit of frozen candidate lists; never reruns retrieval."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/candidate_retrieval_v1'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def main():
    target = OUT / 'audit.json'
    if target.exists():
        raise FileExistsError('Completed audit exists; preserve it')
    registration = read(OUT / 'registration.json')
    summary = read(OUT / 'summary.json')
    cases = read(OUT / 'case_metrics.json')
    complete = read(OUT / 'retrieval_complete.json')
    assert sha(OUT / 'registration.json') == summary['registration_sha256']
    assert sha(OUT / 'retrieval_complete.json') == summary['retrieval_complete_sha256']
    assert all(sha(OUT / name) == digest for name, digest in complete['result_sha256'].items())
    assert len(cases) == len({r['query']['query_id'] for r in cases}) == 200
    assert [r['query'] for r in cases] == registration['queries']
    checked = 0
    exact_ids = set()
    micro = Counter()
    lost = []
    all_pairs = []
    total_exact = 0
    for row in cases:
        qid = row['query']['query_id']
        raw = read(OUT / 'runs' / f'{qid}.json')
        exact = {ident for ident, label in row['known_labels'].items() if label == 'E'}
        exact_ids.update(exact)
        total_exact += len(exact)
        rankings = {'fts_all': raw['all'], 'fts_any': raw['any'],
                    'rrf_all_any': [v['id'] for v in raw['rrf'][:100]]}
        for method, ranking in rankings.items():
            assert len(ranking) == len(set(ranking)) <= 100
            hits = {d for d in exact if d in ranking}
            expected = row['metrics'][method]
            assert math.isclose(len(hits) / len(exact), expected['known_exact_coverage@100'], abs_tol=1e-14)
            assert math.isclose(sum(d in ranking[:20] for d in exact) / len(exact),
                                expected['known_exact_coverage@20'], abs_tol=1e-14)
            assert expected['any_known_exact@100'] == bool(hits)
            assert expected['candidate_count'] == len(ranking)
            micro[method] += len(hits)
            checked += 1
        for left, right in [('fts_all', 'fts_any'), ('fts_any', 'rrf_all_any')]:
            delta = row['metrics'][right]['known_exact_coverage@100'] - row['metrics'][left]['known_exact_coverage@100']
            all_pairs.append((left, right, delta))
        if row['metrics']['rrf_all_any']['known_exact_coverage@100'] < row['metrics']['fts_all']['known_exact_coverage@100']:
            dropped = (exact & set(raw['all'])) - set(rankings['rrf_all_any'])
            ranks = {v['id']: i for i, v in enumerate(raw['rrf'], 1)}
            lost.append({'query': row['query'], 'known_exact': len(exact),
                'lost': [{'id': ident, 'all_rank': raw['all'].index(ident) + 1,
                          'any_rank': raw['any'].index(ident) + 1 if ident in raw['any'] else None,
                          'rrf_rank': ranks[ident]} for ident in sorted(dropped)]})
    for method, values in summary['methods'].items():
        assert math.isclose(statistics.mean(r['metrics'][method]['known_exact_coverage@100'] for r in cases),
                            values['known_exact_coverage@100'], abs_tol=1e-14)
    # Known positive IDs must exist in the eligible locale; this is an ID lookup, not another search.
    with closing(sqlite3.connect((ROOT / 'data/catalog.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        existing = set()
        ids = sorted(exact_ids)
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            existing.update(r[0] for r in db.execute(
                "SELECT id FROM products WHERE locale='us' AND id IN (" + ','.join('?' for _ in batch) + ')', batch))
    assert existing == exact_ids
    comparisons = {}
    for left, right in [('fts_all', 'fts_any'), ('fts_any', 'rrf_all_any')]:
        values = [v for a, b, v in all_pairs if a == left and b == right]
        comparisons[right + '_minus_' + left] = {'mean': statistics.mean(values),
            'positive': sum(v > 0 for v in values), 'equal': sum(v == 0 for v in values),
            'negative': sum(v < 0 for v in values)}
    files = ['registration.json', 'retrieval_complete.json', 'case_metrics.json', 'summary.json']
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
        'scope': 'Post-hoc analysis of frozen development lists, not new evaluation queries or tuning.',
        'script_sha256': sha(Path(__file__)), 'evidence_sha256': {name: sha(OUT / name) for name in files},
        'verified_run_files': len(complete['result_sha256']), 'verified_query_method_pairs': checked,
        'known_exact_query_product_pairs': total_exact, 'unique_exact_ids': len(exact_ids),
        'all_known_exact_ids_present_in_us_catalog': True,
        'micro_known_exact_hits_at100': dict(micro), 'macro_pair_comparisons': comparisons,
        'all_rrf_regressions_vs_all': lost,
        'rrf_regression_cause_supported_by_ranks': 'Known exact hits present in all list were pushed below position 100 by fusion.',
        'new_paid_calls': 0, 'new_retrieval_calls': 0, 'new_gpu_forwards': 0, 'deployment_changed': False}
    with target.open('x', encoding='utf-8') as file:
        file.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'all_rrf_regressions_vs_all'}, indent=2))


if __name__ == '__main__':
    main()

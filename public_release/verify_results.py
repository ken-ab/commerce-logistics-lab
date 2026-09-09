"""Reaggregate the published records; never re-run inference or rewrite study scores."""
from collections import defaultdict
from decimal import Decimal
from copy import deepcopy
import json
import math

from public_release.evidence import ROOT, read, sha, verified_snapshot


def equivalent(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    if type(a) in (int, float) and type(b) in (int, float):
        return math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-11)
    return a == b


def apparel(name, expected):
    directory = ROOT / 'evidence' / name
    summary = read(directory / 'summary.json')
    groups = defaultdict(list)
    for path in sorted((directory / 'runs').glob('*/result.json')):
        row = read(path)
        assert sha(path.with_name('execution.json')) == row['execution_sha256'], str(path)
        assert row['evaluation']['passed'] == all(row['evaluation']['checks'].values()), str(path)
        groups[row.get('condition', row['arm'])].append(row)
    assert set(groups) == set(summary['groups'])
    assert sum(map(len, groups.values())) == expected
    for name, rows in groups.items():
        claim = summary['groups'][name]
        assert len(rows) == claim['runs']
        assert sum(r['evaluation']['passed'] for r in rows) == claim['passed']
        assert sum(Decimal(r['accounted_and_reserved_cny']) for r in rows) == Decimal(claim['cost_cny'])
        for key in ('model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls', 'input_tokens', 'output_tokens', 'delegations'):
            assert sum(r[key] for r in rows) == claim[key], (name, key)
        assert math.isclose(sum(r['latency_seconds'] for r in rows) / len(rows), claim['mean_latency_seconds'])
    return {'records': expected, 'passed': {name: sum(r['evaluation']['passed'] for r in rows) for name, rows in groups.items()}}


def ranking():
    from ranking.metrics import metrics
    directory = ROOT / 'evidence/ranking_runs/20260907T130305394987Z_test_product'
    totals = defaultdict(float)
    count = pairs = 0
    for line in (directory / 'query_results.jsonl').open(encoding='utf-8'):
        row = json.loads(line)
        count += 1
        pairs += len(row['products'])
        labels = [p['label'] for p in row['products']]
        ids = [p['product_id'] for p in row['products']]
        for source, label in [('bm25', 'bm25_candidate_pool'), ('qwen', 'qwen_reranker')]:
            calculated = metrics(labels, [p[source] for p in row['products']], ids)
            assert equivalent(calculated, row['metrics'][label])
            for key in ('ndcg_at_10', 'hit_exact_at_1'):
                totals[label + '/' + key] += calculated[key]
    result = read(directory / 'summary.json')['results']['all']
    assert count == result['queries'] == 14496 and pairs == result['pairs'] == 336373
    for key, value in totals.items():
        arm, metric = key.split('/')
        assert math.isclose(value / count, result[arm][metric], abs_tol=1e-12)
    return {'queries': count, 'pairs': pairs, **{k: v / count for k, v in totals.items()}}


def model_selection():
    from model_selection_100.metrics import summarize
    directory = ROOT / 'evidence/model_selection_100/budget100_v2'
    output = {}
    overlays = read(ROOT / 'public_release/selection_accounting_overlay.json')['records']
    for stage in ('screen', 'shortlist', 'validation'):
        summary = read(directory / (stage + '_summary.json'))['models']
        grouped = defaultdict(list)
        for path in sorted((directory / 'results' / stage).glob('*/*.json')):
            if path.name.endswith('.started.json'):
                continue
            row = read(path)
            if 'model_id' in row:
                response = row['response']
                original_call = response.get('budget_call_id')
                if original_call in overlays and response.get('estimated_cost_cny') is None:
                    row = deepcopy(row)
                    response = row['response']
                    settled = overlays[original_call]
                    response['estimated_cost_cny'] = response['accounted_and_reserved_cny'] = str(Decimal(settled['charged_micro_cny']) / 1_000_000)
                    response['usage'] = settled['usage']
                    response['accounting_reconciliation'] = 'Settled ledger overlay; original response file unchanged'
                grouped[row['model_id']].append(row)
        assert set(summary) == set(grouped), stage
        for model, expected in summary.items():
            actual = summarize(grouped[model], expected['expected_queries'])
            assert equivalent(actual, expected), (stage, model)
        output[stage] = {'models': len(summary), 'submitted': sum(r.get('submitted', False) for rows in grouped.values() for r in rows)}
    assert output['screen']['models'] == 100
    selected = read(directory / 'validation_readout.json')
    assert selected['complete'] and selected['winner'] == 'qwen3.8-flash' and selected['gate']['replace_current_model']
    return output


def rationale():
    from research.audit_apparel_rationale_recovery import statistics, failure_type
    from collections import Counter
    directory = ROOT / 'evidence/apparel_rationale_account_recovery_v1'
    summary = read(directory / 'summary.json')
    rows = read(directory / 'combined_results.json')
    receipt = read(ROOT / 'evidence/apparel_rationale_recovery_check_20260909.json')
    inputs = {r['id']: r for r in read(ROOT / 'evidence/apparel_rationale_audit_v2/inputs.json')}
    source_map = read(ROOT / 'evidence/apparel_rationale_frozen_source/source_map.json')
    omitted = read(ROOT / 'evidence/apparel_rationale_frozen_source/private_provenance_omissions.json')
    registration = read(directory / 'registration.json')
    for name, digest in registration['source_sha256'].items():
        if name in omitted:
            assert name.startswith('knowledge/') and omitted[name]['sha256'] == digest
            continue
        target = ROOT / (source_map[name]['path'] if name in source_map else name)
        assert sha(target) == digest, name
    assert len(rows) == 144 and statistics(rows) == receipt['combined']
    assert sha(directory / 'combined_results.json') == summary['combined_results_sha256']
    assert set(r['id'] for r in rows) == set(inputs)
    for row in rows:
        origin = summary['result_origins'][row['id']]
        assert sha(ROOT / origin['path']) == origin['sha256']
        assert read(ROOT / origin['path']) == row
        if row['status'] == 'audited':
            quotes = ''.join(c['quote'] for c in row['decision']['claims'])
            assert ''.join(quotes.split()) == ''.join(inputs[row['id']]['answer'].split())
    for condition, expected in summary['groups'].items():
        assert statistics([r for r in rows if r['condition'] == condition]) == expected
    errors = dict(Counter(failure_type(r) for r in rows if r['status'] == 'audit_failed'))
    assert errors == receipt['failure_categories']
    calls = read(directory / 'study_accounting_export.json')['records']
    assert len(calls) == 220
    settled = sum(r['charged'] or 0 for r in calls)
    reserved = sum(r['reserved'] for r in calls if r['charged'] is None)
    assert settled == receipt['study_settled_micro_cny'] == 31_015_270
    assert reserved == receipt['study_uncertain_reserved_micro_cny'] == 61_501_708
    reviews = []
    for name in ('apparel_rationale_flag_review_20260909.json', 'apparel_rationale_recovery_flag_review_20260909.json'):
        reviews.extend(read(ROOT / 'evidence' / name)['records'])
    assert {r['id'] for r in reviews} == {r['id'] for r in rows if r.get('decision', {}).get('verdict') == 'unsupported'}
    return {'registered': len(rows), 'valid_judgments': 105, 'supported_judgments': 91,
            'unsupported_judgments': 14, 'audit_failures': errors, 'missing_original_explanation': 1,
            'targeted_review_not_independent_human_accuracy': dict(Counter(r['classification'] for r in reviews)),
            'settled_cny': settled / 1_000_000, 'uncertain_reserved_cny': reserved / 1_000_000,
            'private_reading_receipts_omitted': len(omitted)}


def main():
    verified_snapshot()
    return {'apparel_expansion': apparel('apparel_expansion_study_v1', 216),
            'proposal_reliability': apparel('apparel_reliability_study_v1', 144),
            'ranking': ranking(), 'model_selection': model_selection(), 'rationale_audit': rationale(),
            'scope': 'Integrity and metric reaggregation from published records; no new inference or universal factual accuracy claim.',
            'paid_calls': 0}


if __name__ == '__main__':
    print(json.dumps(main(), ensure_ascii=False, indent=2))

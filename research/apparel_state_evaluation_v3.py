"""Independent scenario evaluation, not the runtime operation-check result."""
from copy import deepcopy

from research.apparel_evaluation import score
from research.apparel_task_audit_fixed import task_audit


def line_checks(case, result):
    expected = case['expected']['allowed_by_line']
    actual = result['after']['selections']
    failure = []
    if (len(actual) != len(expected) or len({p['line_id'] for p in actual}) != len(actual)
            or {p['line_id'] for p in actual} != set(expected)
            or any(p['sku'] not in expected.get(p['line_id'], []) for p in actual)):
        failure.append('scenario_line_assignment_wrong')
    decision = result['report']['decision'] if result.get('report') else {}
    if case['contract']['mode'] != 'inspect_product':
        pairs = lambda rows: sorted((p['line_id'], p['sku']) for p in rows)
        if decision.get('selection_snapshot') is None or pairs(decision['selection_snapshot']) != pairs(actual):
            failure.append('reported_line_assignment_not_actual')
        if set(decision.get('product_skus', [])) != {p['sku'] for p in actual}:
            failure.append('reported_sku_set_not_actual')
    return failure


def evaluate(case, result, store, now):
    original = score(case, result, store, now)
    supplementary = task_audit(case, result | {'score': original})
    current = deepcopy(supplementary)
    current['failure_reasons'] = sorted(set(current['failure_reasons'] + line_checks(case, result)))
    current['business_success'] = not current['failure_reasons'] and not current['constraint_violations']
    current['task_completed'] = current['business_success'] and current['required_evidence_covered']
    current['notice'] = 'Same frozen semantic audit plus pre-registered per-line expected/actual checks. Runtime operation_check is not used as a scoring oracle.'
    return {'original_score': original, 'supplementary_score': supplementary, 'score': current}

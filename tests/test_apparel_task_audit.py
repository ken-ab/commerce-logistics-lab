from copy import deepcopy

from research.apparel_task_audit import task_audit


def record():
    options = [{'requires_user_choice': True, 'minimum_shipping_budget_cents': 20000, 'earliest_delivery_deadline_at': '2026-10-01T00:00:00Z'}]
    return {'score': {'failure_reasons': [], 'evidence_gaps': ['adjustment_options_not_cited'], 'constraint_violations': [],
                      'business_success': True, 'required_evidence_covered': False, 'task_completed': False},
            'observations': {'O-1': {'observation_id': 'O-1', 'success': True, 'tool': 'prepare_proposal',
                                   'result': {'proposal_id': 'P', 'route': {'adjustment_options': options}}}},
            'before': {'proposals': []}, 'after': {'selections': [{'sku': 'S'}], 'proposals': [{'proposal_id': 'P', 'route': {'adjustment_options': options}}]},
            'report': {'source_facts': []}}


def test_one_complete_option_is_enough_but_incomplete_constraints_are_not():
    value = record(); option = value['after']['proposals'][0]['route']['adjustment_options'][0]
    value['report']['source_facts'] = [{'observation_id': 'O-1', 'pointer': '/result/route/adjustment_options/0', 'value': option}]
    assessed = task_audit({'family': 'shipping_infeasible'}, value)
    assert assessed['task_completed'] and not value['score']['task_completed']
    value['report']['source_facts'] = [{'observation_id': 'O-1', 'pointer': '/result/route/adjustment_options/0/minimum_shipping_budget_cents', 'value': 20000}]
    assert not task_audit({'family': 'shipping_infeasible'}, value)['task_completed']


def test_real_candidate_failure_and_constraint_violation_are_never_removed():
    value = record()
    value['score']['failure_reasons'] = ['wrong_or_nonminimal_candidate']
    value['score']['constraint_violations'] = ['requirements_changed']
    result = task_audit({'family': 'shortage_alternative'}, value)
    assert 'wrong_or_nonminimal_candidate' in result['failure_reasons']
    assert result['constraint_violations'] == ['requirements_changed'] and not result['task_completed']

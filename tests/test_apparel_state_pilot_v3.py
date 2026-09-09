from copy import deepcopy
import json

import pytest

from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import iso
from research.apparel_state_cases_v3 import cases, setup_case
from research.apparel_state_evaluation_v3 import line_checks
from research.apparel_state_pilot_v3 import fixture_check


@pytest.mark.parametrize('case', cases(), ids=lambda c: c['scenario'])
def test_development_case_is_feasible_without_model_and_hides_expected_labels(tmp_path, case):
    TaskContract.model_validate(case['contract'])
    store, draft_id, now = setup_case(case, tmp_path / 'operations.sqlite')
    initial = store.view('evaluation', draft_id)
    seed = {'draft_id': draft_id, 'now': iso(now), 'initial_view_digest': digest(initial)}
    content = json.dumps({'state': initial, 'world': store.base_world, 'events': store.transport_events()})
    assert case['id'] not in content
    assert 'allowed_by_line' not in content and 'minimum_differences' not in content
    assert fixture_check(case, tmp_path, seed, store.base_world)['passed']
    assert digest(store.view('evaluation', draft_id)) == seed['initial_view_digest']


def test_external_scorer_rejects_swapped_lines_even_with_the_same_sku_set():
    case = next(c for c in cases() if c['scenario'] == 'multiple_lines')
    right = [{'line_id': line, 'sku': allowed[0]} for line, allowed in case['expected']['allowed_by_line'].items()]
    wrong = [{'line_id': right[0]['line_id'], 'sku': right[1]['sku']}, {'line_id': right[1]['line_id'], 'sku': right[0]['sku']}]
    result = {'after': {'selections': wrong}, 'report': {'decision': {
        'selection_snapshot': wrong, 'product_skus': [p['sku'] for p in wrong]}}}
    assert line_checks(case, result) == ['scenario_line_assignment_wrong']
    result['after']['selections'] = right
    assert line_checks(case, result) == ['reported_line_assignment_not_actual']
    result['report']['decision']['selection_snapshot'] = right
    assert line_checks(case, result) == []


def test_expected_alternatives_accept_ties_but_not_unstaged_claims():
    case = next(c for c in cases() if c['scenario'] == 'shortage_alternative')
    for alternative in case['expected']['selected_skus']:
        pairs = [{'line_id': 'item-1', 'sku': alternative}]
        result = {'after': {'selections': pairs}, 'report': {'decision': {
            'selection_snapshot': pairs, 'product_skus': [alternative]}}}
        assert line_checks(case, result) == []
    unstaged = deepcopy(result)
    unstaged['after']['selections'] = [{'line_id': 'item-1', 'sku': case['target_sku']}]
    assert set(line_checks(case, unstaged)) == {'scenario_line_assignment_wrong',
                                              'reported_line_assignment_not_actual', 'reported_sku_set_not_actual'}

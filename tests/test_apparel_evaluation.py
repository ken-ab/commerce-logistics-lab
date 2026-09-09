from copy import deepcopy
import json

from apparel_fulfillment.agent import ApparelAgent
from research.apparel_cases import make, setup
from research.apparel_evaluation import score
from test_apparel_agent import Client, tool, finish


def test_target_splits_and_case_ids_are_disjoint():
    validation, test = make('validation', 2), make('test', 12)
    assert not set(validation['target_pool']) & set(test['target_pool'])
    assert not {c['id'] for c in validation['cases']} & {c['id'] for c in test['cases']}
    assert len(test['cases']) == 108 and len({c['id'] for c in test['cases']}) == 108
    assert {c['scenario_kind'] for c in test['cases']} >= {'cancel', 'delay', 'unrelated', 'missed'}


def test_information_success_and_wrong_evidence_are_distinguished(tmp_path):
    case = next(c for c in make('validation', 2)['cases'] if c['family'] == 'product_info' and c['expected']['info_field'] == 'size')
    store, ident, now = setup(case, tmp_path / 'info.sqlite')
    def complete(messages):
        return [tool('finish', status='information', product_skus=[case['target_sku']], proposal_id=None,
                     question_codes=[], citations=[{'observation_id': 'O-1', 'pointer': '/result/variant/size'}], rationale='Read the requested field.')]
    result = ApparelAgent(store, 'evaluation', ident, client=Client([[tool('read_variant', sku=case['target_sku'])], complete]), now=now).run(case['task'])
    assert score(case, result, store, now)['task_completed']
    wrong = deepcopy(result)
    wrong['report']['source_facts'][0].update(pointer='/result/variant/brand', value='Unrelated field')
    assessed = score(case, wrong, store, now)
    assert assessed['business_success'] and not assessed['task_completed']
    assert 'requested_product_field_not_cited' in assessed['evidence_gaps']


def test_no_completed_report_and_changed_requirements_cannot_pass(tmp_path):
    case = next(c for c in make('validation', 2)['cases'] if c['family'] == 'rule_blocked')
    store, ident, now = setup(case, tmp_path / 'blocked.sqlite')
    result = ApparelAgent(store, 'evaluation', ident, client=Client([]), now=now).run(case['task'])
    result['after']['request']['sales_region'] = 'GB'
    assessed = score(case, result, store, now)
    assert not assessed['task_completed'] and assessed['constraint_violation']
    assert 'requirements_changed' in assessed['constraint_violations']

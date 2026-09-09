from copy import deepcopy
import json

import pytest

from apparel_fulfillment.action_contract import TaskContract, covers, finish_errors
from apparel_fulfillment.agent import MAX_MODEL_CALLS
from apparel_fulfillment.agent_state_v3 import StateContractAgent, VERSION
from apparel_fulfillment.store import ApparelStore
from test_apparel_agent import Client, finish as base_finish, tool, workspace
from test_apparel_orders import fixture, request
from test_apparel_transport import NOW, SHIP


def finish(*args, **kwargs):
    step = base_finish(*args, **kwargs)
    value = json.loads(step[0]['function']['arguments'])
    value['selection_snapshot'] = None if value['status'] == 'information' else [
        {'line_id': 'one', 'sku': sku} for sku in value['product_skus']]
    step[0]['function']['arguments'] = json.dumps(value)
    return step


def decision(status='ready', products=None, proposal=None, citations=()):
    return [tool('finish', status=status, product_skus=products or [], proposal_id=proposal, question_codes=[],
                 selection_snapshot=[{'line_id': 'one', 'sku': sku} for sku in products or []],
                 citations=[{'observation_id': i, 'pointer': p} for i, p in citations],
                 rationale='Report only observed business state; do not confirm this order.')]


def shipping_workspace(tmp_path, *, cancel=False):
    world = fixture()
    for v in world['variants'].values():
        v['provenance']['weight_grams_per_catalog_unit'] = {'evidence_id': 'sim-weight'}
    store = ApparelStore(tmp_path / 'shipping.sqlite', world=world)
    req = request(); req.update(needs_shipping=True, shipping=deepcopy(SHIP))
    draft = store.create_draft('owner', req)
    draft = store.select('owner', draft['id'], [{'line_id': 'one', 'sku': 'A'}], expected_revision=draft['revision'])
    p = store.propose('owner', draft['id'], expected_revision=draft['revision'], now=NOW)
    if cancel:
        flight = next(s for s in p['route']['segments'] if s['mode'] == 'air')
        store.add_transport_event({'event_id': 'cancel-v3', 'kind': 'cancel', 'leg_id': flight['leg_id'],
                                   'nominal_departure': flight['nominal_departure'], 'published_at': '2026-09-08T00:00:00Z'})
    return store, draft, p


def test_bootstrap_contains_actual_order_identity_and_counts_as_a_read(workspace):
    store, draft = workspace
    def first(messages):
        x = json.loads(messages[1]['content'])
        assert x['initial_order_observation']['result']['id'] == draft['id']
        assert x['initial_order_observation']['observation_id'] == 'O-1'
        assert x['operation_contract']['product_sku'] == 'A'
        return [tool('read_variant', sku='A')]
    client = Client([first, finish(status='information', observation='O-2', pointer='/result/variant/size')])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'inspect_product', 'product_sku': 'A', 'product_fields': ['size']}).run('只查A尺码')
    assert r['policy_version'] == VERSION and r['run_status'] == 'completed'
    assert r['host_initial_reads'] == 1 and r['tool_calls'] == 2
    assert r['report']['operation_check']['passed']
    assert r['after']['selections'] == []


def test_unperformed_selection_is_rejected_and_agent_must_act(workspace):
    store, draft = workspace
    def actually_stage(messages):
        assert store.view('owner', draft['id'])['selections'] == []
        rejected = json.loads(messages[-1]['content'])
        assert any(e['reason'] == 'reported_selection_not_current' for e in rejected['invalid'])
        return [tool('select_variants', selections=[{'line_id': 'one', 'sku': 'A'}], expected_revision=1)]
    client = Client([[tool('read_variant', sku='A')], finish(observation='O-2', pointer='/result/variant/sku'),
                     actually_stage, finish(observation='O-3')])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'stage_candidate', 'line_id': 'one'}).run('将A选入草稿供审阅')
    assert r['run_status'] == 'completed' and r['model_calls'] == 4
    assert r['after']['selections'] == [{'line_id': 'one', 'sku': 'A'}]
    assert r['report']['execution_receipt']['successful_write_observations'] == [{'observation_id': 'O-3', 'tool': 'select_variants'}]
    assert r['after']['confirmation'] is None


def test_existing_specific_approval_is_usable_without_requesting_it_again(workspace):
    store, draft = workspace
    draft = store.select('owner', draft['id'], [{'line_id': 'one', 'sku': 'B'}], expected_revision=draft['revision'])
    approval = draft['order_check']['substitution_proposals'][0]['approval_id']
    draft = store.approve_substitution('owner', draft['id'], approval, expected_revision=draft['revision'])
    def result_from_state(messages):
        obs = json.loads(messages[1]['content'])['initial_order_observation']
        assert obs['result']['request']['lines'][0]['brand'] == 'BrandA'
        assert obs['result']['selections'][0]['sku'] == 'B'
        assert obs['result']['approved_substitutions'][0]['approval_id'] == approval
        return finish(products=['B'])
    r = StateContractAgent(store, 'owner', draft['id'], client=Client([result_from_state]), now=NOW,
                           contract={'mode': 'check_order'}).run('核验已经批准的替代')
    assert r['run_status'] == 'completed' and r['model_calls'] == 1
    assert r['after']['approved_substitutions'] == r['before']['approved_substitutions']


def test_read_only_contract_stops_writes_before_they_happen(workspace):
    store, draft = workspace
    client = Client([[tool('read_variant', sku='A'), tool('select_variants', selections=[{'line_id': 'one', 'sku': 'A'}], expected_revision=1)],
                     finish(status='information', observation='O-2', pointer='/result/variant/size')])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'inspect_product', 'product_sku': 'A', 'product_fields': ['size']}).run('只查A尺码')
    assert r['run_status'] == 'completed' and r['observations']['O-3']['success'] is False
    assert r['after']['selections'] == [] and r['after']['revision'] == 1


def test_review_requires_observed_validity_and_does_not_revise_valid_proposal(tmp_path):
    store, draft, old = shipping_workspace(tmp_path)
    citations = [('O-1', '/result/order_check/status'), ('O-3', '/result/validity/valid'),
                 ('O-3', '/result/proposal/route/total_cost_cents'), ('O-3', '/result/proposal/route/arrival_at')]
    client = Client([[tool('prepare_proposal', expected_revision=draft['revision'])],
                     [tool('read_proposal', proposal_id=old['proposal_id'])],
                     [tool('prepare_proposal', expected_revision=draft['revision'])],
                     decision(products=['A'], proposal=old['proposal_id'], citations=citations)])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'review_proposal'}).run('检查现有提案，有效就保留')
    assert r['run_status'] == 'completed'
    assert not r['observations']['O-2']['success'] and not r['observations']['O-4']['success']
    assert len(r['after']['proposals']) == 1


def test_old_proposal_route_fields_cannot_support_new_version(tmp_path):
    store, draft, old = shipping_workspace(tmp_path, cancel=True)
    new_id = {}
    def wrong_route_fields(messages):
        new_id['value'] = json.loads(messages[-1]['content'])['result']['proposal_id']
        return decision(products=['A'], proposal=new_id['value'], citations=[('O-1', '/result/order_check/status'),
            ('O-2', '/result/validity/valid'), ('O-2', '/result/proposal/route/total_cost_cents'), ('O-2', '/result/proposal/route/arrival_at')])
    def corrected(messages):
        assert any(e['reason'] == 'current_route_field_not_supported' for e in json.loads(messages[-1]['content'])['invalid'])
        return decision(products=['A'], proposal=new_id['value'], citations=[('O-1', '/result/order_check/status'),
            ('O-2', '/result/validity/valid'), ('O-3', '/result/route/total_cost_cents'), ('O-3', '/result/route/arrival_at')])
    client = Client([[tool('read_proposal', proposal_id=old['proposal_id'])],
                     [tool('prepare_proposal', expected_revision=draft['revision'])], wrong_route_fields, corrected])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'review_proposal'}).run('取消后修订提案')
    assert r['run_status'] == 'completed' and r['model_calls'] == 4
    assert r['report']['operation_check']['passed']
    assert len(r['after']['proposals']) == 2 and r['after']['confirmation'] is None


def test_parent_citation_covers_field_but_sibling_or_wrong_identity_does_not():
    facts = [{'observation_id': 'O-1', 'pointer': '/result/order_check', 'value': {'status': 'ready', 'issues': []}}]
    assert covers(facts, 'O-1', '/result/order_check/status', 'ready')
    assert not covers(facts, 'O-2', '/result/order_check/status', 'ready')
    assert not covers(facts, 'O-1', '/result/other/status', 'ready')
    assert not covers([{'observation_id': 'O-1', 'pointer': '/result/valid', 'value': True}], 'O-1', '/result/valid', 1)


def test_control_records_violation_without_enforcing_or_silently_repairing(workspace):
    store, draft = workspace
    r = StateContractAgent(store, 'owner', draft['id'], client=Client([finish(products=[])]), now=NOW,
                           contract={'mode': 'check_order'}, enforce_contract=False).run('核验订单')
    assert r['run_status'] == 'completed'
    assert r['report']['operation_check']['passed'] is False
    assert r['report']['operation_check']['enforced'] is False
    assert r['after']['selections'] == []


def test_repeated_rejected_completion_stops_at_shared_cap_without_hidden_actions(workspace):
    store, draft = workspace
    r = StateContractAgent(store, 'owner', draft['id'], client=Client([finish(products=[])] * MAX_MODEL_CALLS), now=NOW,
                           contract={'mode': 'check_order'}).run('核验订单')
    assert r['run_status'] == 'call_limit' and r['model_calls'] == MAX_MODEL_CALLS
    assert r['report'] is None and r['tool_calls'] == 1
    assert r['after']['selections'] == []


def test_legitimate_original_stock_comparison_and_alternative_source_are_accepted(tmp_path):
    world = fixture(); world['stock']['A']['available_catalog_units'] = 0
    store = ApparelStore(tmp_path / 'alternatives.sqlite', world=world)
    draft = store.create_draft('owner', request(requested_sku='A'))
    draft = store.select('owner', draft['id'], [{'line_id': 'one', 'sku': 'A'}], expected_revision=1)
    client = Client([[tool('read_variant', sku='A'), tool('find_alternatives', line_id='one')],
                     [tool('select_variants', selections=[{'line_id': 'one', 'sku': 'B'}], expected_revision=2)],
                     decision(status='needs_clarification', products=['B'], citations=[('O-2', '/result/stock/available_catalog_units'),
                         ('O-3', '/result/alternatives/0/differences'), ('O-4', '/result/order_check/status'), ('O-4', '/result/order_check/issues')])])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'stage_candidate', 'line_id': 'one'}).run('将最少改动候选选入待审阅')
    assert r['run_status'] == 'completed' and r['report']['operation_check']['passed']
    assert r['after']['approved_substitutions'] == [] and r['after']['confirmation'] is None


def test_contract_requires_caller_operation_fields_instead_of_guessing_test_labels():
    with pytest.raises(ValueError): TaskContract(mode='inspect_product')
    with pytest.raises(ValueError): TaskContract(mode='stage_candidate')
    with pytest.raises(ValueError): TaskContract(mode='check_order', expected_status='ready')
    with pytest.raises(ValueError): TaskContract(mode='check_order', proposal_id='x')


def test_same_sku_set_with_wrong_line_assignments_is_rejected(workspace):
    store, draft = workspace
    initial = store.view('owner', draft['id'])
    current = deepcopy(initial)
    current['selections'] = [{'line_id': 'one', 'sku': 'A'}, {'line_id': 'two', 'sku': 'B'}]
    report = {'decision': {'status': 'needs_clarification', 'product_skus': ['A', 'B'], 'proposal_id': None,
                          'selection_snapshot': [{'line_id': 'one', 'sku': 'B'}, {'line_id': 'two', 'sku': 'A'}]},
              'source_facts': []}
    errors = finish_errors(TaskContract(mode='stage_candidate', line_id='one'), initial, current, {}, report)
    assert any(e['reason'] == 'reported_line_selections_not_current' for e in errors)
    assert not any(e['reason'] == 'reported_selection_not_current' for e in errors)


def test_infeasible_route_is_valid_unfulfillable_outcome_with_actionable_option(tmp_path):
    world = fixture()
    for v in world['variants'].values(): v['provenance']['weight_grams_per_catalog_unit'] = {'evidence_id': 'sim-weight'}
    store = ApparelStore(tmp_path / 'infeasible.sqlite', world=world)
    req = request(); req.update(needs_shipping=True, shipping=deepcopy(SHIP)); req['shipping']['budget_cents'] = 1
    draft = store.create_draft('owner', req)
    draft = store.select('owner', draft['id'], [{'line_id': 'one', 'sku': 'A'}], expected_revision=1)
    def no_route(messages):
        p = json.loads(messages[-1]['content'])['result']
        assert p['route']['status'] == 'infeasible'
        return decision(status='unfulfillable', products=['A'], proposal=p['proposal_id'], citations=[
            ('O-1', '/result/order_check/status'), ('O-2', '/result/route/status'), ('O-2', '/result/route/adjustment_options/0')])
    r = StateContractAgent(store, 'owner', draft['id'], client=Client([[tool('prepare_proposal', expected_revision=2)], no_route]),
                           now=NOW, contract={'mode': 'prepare_proposal'}).run('预算不足时说明无解及调整方向')
    assert r['run_status'] == 'completed' and r['report']['operation_check']['passed']
    assert r['report']['decision']['status'] == 'unfulfillable'
    assert r['after']['request']['shipping']['budget_cents'] == 1


def test_inventory_version_change_requires_fresh_order_evidence(workspace):
    store, draft = workspace
    draft = store.select('owner', draft['id'], [{'line_id': 'one', 'sku': 'A'}], expected_revision=1)
    def external_update_then_stale_report(messages):
        store.set_simulated_stock('A', 21, expected_version=1)
        return finish()
    client = Client([external_update_then_stale_report, [tool('read_order')], finish(observation='O-2')])
    r = StateContractAgent(store, 'owner', draft['id'], client=client, now=NOW,
                           contract={'mode': 'check_order'}).run('核验当前订单状态')
    assert r['run_status'] == 'completed' and r['model_calls'] == 3
    rejections = [t for t in r['traces'] if t['kind'] == 'report_rejected']
    assert len(rejections) == 1
    assert any(e['reason'] == 'current_order_status_not_supported' for e in rejections[0]['invalid'])

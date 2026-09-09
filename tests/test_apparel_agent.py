"""Actual orchestration control paths with scripted provider responses."""
from contextlib import closing
import json

import pytest

from apparel_fulfillment.agent import ApparelAgent, MAX_MODEL_CALLS
from apparel_fulfillment.store import ApparelStore
from test_apparel_orders import fixture, request
from test_apparel_transport import NOW


def tool(name, **args):
    return {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class Client:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.inputs = []

    def chat(self, messages, **kwargs):
        self.inputs.append((messages.copy(), kwargs))
        step = next(self.steps)
        if callable(step): step = step(messages)
        return {'message': {'role': 'assistant', 'content': '', 'tool_calls': step}, 'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
                'estimated_cost_cny': '0.001', 'latency_seconds': 0.01, 'requested_model': kwargs['model'],
                'returned_model': kwargs['model'], 'finish_reason': 'tool_calls'}


@pytest.fixture
def workspace(tmp_path):
    world = fixture()
    for v in world['variants'].values(): v['provenance']['weight_grams_per_catalog_unit'] = {'evidence_id': 'test-weight'}
    store = ApparelStore(tmp_path / 'agent.sqlite', world=world)
    draft = store.create_draft('owner', request())
    return store, draft


def finish(status='ready', proposal=None, observation='O-1', pointer='/result/order_check/status', products=None):
    return [tool('finish', status=status, product_skus=['A'] if products is None else products, proposal_id=proposal, question_codes=[],
                 citations=[{'observation_id': observation, 'pointer': pointer}], rationale='Directly verified the requested source and constraints.')]


def proposal_finish(messages):
    payload = json.loads(messages[-1]['content'])['result']
    return finish(proposal=payload['proposal_id'], observation='O-4', pointer='/result/route/status')


def steps():
    return [[tool('read_order'), tool('read_variant', sku='A')],
            [tool('select_variants', selections=[{'line_id': 'one', 'sku': 'A'}], expected_revision=1)],
            [tool('prepare_proposal', expected_revision=2)], proposal_finish]


def test_single_agent_uses_sources_and_stages_without_confirmation(workspace):
    store, draft = workspace
    client = Client(steps())
    result = ApparelAgent(store, 'owner', draft['id'], client=client, now=NOW).run('核验这笔订单并准备提案，不需要运输。')
    assert result['run_status'] == 'completed' and result['model_calls'] == 4
    assert result['report']['grounding']['supported'] == 1
    assert len(result['after']['proposals']) == 1 and result['after']['confirmation'] is None
    with closing(store.connect()) as db:
        assert db.execute("SELECT quantity FROM inventory WHERE sku='A'").fetchone()[0] == 20
    assert all(c[1]['model'] == 'gpt-5.6-luna' for c in client.inputs)


def test_coordinator_executes_real_expert_loop_in_shared_budget(workspace):
    store, draft = workspace
    def root_finish(messages):
        decision = json.loads(messages[-1]['content'])['expert_report']['decision']
        return [tool('finish', **decision)]
    client = Client([[tool('delegate', expert='product', task='Check the order and prepare its no-shipping proposal.', reason='Apparel order verification is required.')], *steps(), root_finish])
    result = ApparelAgent(store, 'owner', draft['id'], client=client, arm='coordinator', now=NOW).run('核验并准备订单提案。')
    assert result['run_status'] == 'completed' and result['model_calls'] == 6 and result['delegations'] == 1
    assert [c['role'] for c in result['calls']] == ['coordinator', 'product', 'product', 'product', 'product', 'coordinator']
    root_tools = [t['function']['name'] for t in client.inputs[0][1]['tools']]
    assert 'select_variants' not in root_tools and 'delegate' in root_tools


def test_on_demand_direct_execution_records_observed_rationale(workspace):
    store, draft = workspace
    client = Client([[tool('read_order')], [tool('record_routing', mode='direct', reason='A single variant and no transport are involved.'), tool('read_variant', sku='A')], finish(status='information', observation='O-2', pointer='/result/variant/size')])
    result = ApparelAgent(store, 'owner', draft['id'], client=client, arm='on_demand', now=NOW).run('查询 A 的尺码即可。')
    assert result['run_status'] == 'completed' and result['delegations'] == 0
    routing = next(t for t in result['traces'] if t['kind'] == 'routing')
    assert routing['mode'] == 'direct' and routing['observed_ids'] == ['O-1']
    assert result['after']['selections'] == []


def test_model_cannot_approve_or_confirm_and_invalid_citations_are_retained(workspace):
    store, draft = workspace
    client = Client([[tool('approve_substitution', approval_id='fake'), tool('confirm', proposal_id='fake'), tool('read_order')], finish(status='needs_clarification', observation='O-999', products=[]), finish(status='needs_clarification', products=[])])
    result = ApparelAgent(store, 'owner', draft['id'], client=client, now=NOW).run('确认订单')
    assert result['after']['confirmation'] is None
    assert len([t for t in result['traces'] if t['kind'] == 'tool_rejected']) == 2
    assert result['report']['grounding']['supported'] == 1
    rejected = next(t for t in result['traces'] if t['kind'] == 'report_rejected')
    assert rejected['invalid'][0]['observation_id'] == 'O-999'


def test_failed_and_exhausted_runs_remain_in_denominator(workspace):
    store, draft = workspace
    class Failure:
        def chat(self, *args, **kwargs): raise RuntimeError('isolated provider failure')
    failed = ApparelAgent(store, 'owner', draft['id'], client=Failure(), now=NOW).run('读取订单')
    assert failed['run_status'] == 'failed' and failed['model_calls'] == 1 and failed['successful_model_calls'] == 0
    assert failed['calls'][0]['error_message'] == 'isolated provider failure'
    capped = ApparelAgent(store, 'owner', draft['id'], client=Client([[]] * MAX_MODEL_CALLS), now=NOW).run('读取订单')
    assert capped['run_status'] == 'call_limit' and capped['model_calls'] == MAX_MODEL_CALLS
    assert capped['report'] is None


def test_no_shipping_order_cannot_invoke_transport_specific_tool(workspace):
    store, draft = workspace
    client = Client([[tool('read_transport_events'), tool('read_order')], finish(status='needs_clarification', observation='O-2', products=[])])
    result = ApparelAgent(store, 'owner', draft['id'], client=client, now=NOW).run('读取订单')
    assert result['observations']['O-1']['success'] is False
    assert result['successful_tool_calls'] == 1 and result['tool_calls'] == 2


def test_unobserved_identity_is_rejected_before_report_completion(workspace):
    store, draft = workspace
    client = Client([[tool('read_variant', sku='A')], finish(status='information', observation='O-1', pointer='/result/variant/size', products=['invented']),
                     finish(status='information', observation='O-1', pointer='/result/variant/size')])
    result = ApparelAgent(store, 'owner', draft['id'], client=client, now=NOW).run('查询尺码')
    assert result['run_status'] == 'completed' and result['model_calls'] == 3
    rejected = next(t for t in result['traces'] if t['kind'] == 'report_rejected')
    assert rejected['invalid'][0]['value'] == 'invented'

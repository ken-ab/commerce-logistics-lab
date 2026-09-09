import asyncio
from contextlib import closing
import json

import pytest

from commerce_lab.agent import CommerceAgent
from commerce_lab.skills import BASELINE
from commerce_lab.state import BusinessError, Store, WAREHOUSES
from commerce_lab_v2.agent import IdentityAwareCommerceAgent


class FixtureCatalog:
    def get(self, ident):
        if ident != 'us:IDENTITY-FIXTURE':
            return None
        return {'id': ident, 'title': 'Fixture item', 'price_usd': 10.0,
                'weight_kg': 0.5, 'evidence_id': 'fixture:' + ident}


class ScriptedClient:
    config = {'COMMERCE_MODEL': 'offline-script'}

    def __init__(self, messages):
        self.messages = iter(messages)

    def chat(self, *args, **kwargs):
        message = next(self.messages)
        return {'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop',
                'estimated_cost_cny': '0', 'requested_model': 'offline-script'}


def tool(name, **args):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'fixture-call', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}]}


def answer(ident='us:IDENTITY-FIXTURE', proposal_id=None):
    return {'role': 'assistant', 'content': json.dumps({
        'answer': 'The supplied stock is insufficient; the cart is unchanged.',
        'product_ids': [ident], 'proposal_id': proposal_id, 'status': 'infeasible'})}


def make_store(path):
    store = Store(FixtureCatalog(), path)
    sid = store.session()['id']
    with closing(store.connect()) as db, db:
        db.executemany('INSERT INTO stock VALUES (?,?,?,1)',
                       [('us:IDENTITY-FIXTURE', wh, 1) for wh in WAREHOUSES])
    return store, sid


def run_agent(cls, store, sid, messages):
    agent = cls(store=store, client=ScriptedClient(messages), policy=BASELINE, topology='single')
    return agent, asyncio.run(agent.run(sid, 'Check stock for three units, leaving an insufficient cart unchanged.'))


def test_stock_lookup_can_support_identity_without_granting_cart_write(tmp_path):
    store, sid = make_store(tmp_path / 'v2.sqlite')
    agent, result = run_agent(IdentityAwareCommerceAgent, store, sid, [
        tool('get_stock', product_id='us:IDENTITY-FIXTURE'), answer()])
    assert 'error' not in result
    assert result['grounding']['identity_evidence'] == {'us:IDENTITY-FIXTURE': 'stock_record_identity_only'}
    assert store.cart(sid)['items'] == []
    with pytest.raises(BusinessError, match='Read this product'):
        store.change_cart(sid, 'us:IDENTITY-FIXTURE', 1)
    record = store.run(sid, result['run_id'])
    assert any(t['kind'] == 'evidence_contract_configuration' for t in record['traces'])
    assert agent.product_ids == set()


def test_original_version_keeps_its_recorded_contract(tmp_path):
    store, sid = make_store(tmp_path / 'v1.sqlite')
    _, result = run_agent(CommerceAgent, store, sid, [
        tool('get_stock', product_id='us:IDENTITY-FIXTURE'), answer()])
    assert result['error'] == 'Final product list includes an unobserved catalog ID'


def test_failed_unknown_stock_does_not_establish_identity(tmp_path):
    store, sid = make_store(tmp_path / 'unknown.sqlite')
    agent, result = run_agent(IdentityAwareCommerceAgent, store, sid, [
        tool('get_stock', product_id='us:UNKNOWN'), answer('us:UNKNOWN')])
    assert result['error'] == 'Final product list includes an unobserved catalog ID'
    assert agent.stock_identity_ids == set()


def test_stock_identity_does_not_approve_fabricated_proposal(tmp_path):
    store, sid = make_store(tmp_path / 'proposal.sqlite')
    _, result = run_agent(IdentityAwareCommerceAgent, store, sid, [
        tool('get_stock', product_id='us:IDENTITY-FIXTURE'), answer(proposal_id='fake-proposal')])
    assert result['error'] == 'Final proposal ID was not produced by this run'


def test_stock_observation_does_not_carry_over_to_another_run(tmp_path):
    store, sid = make_store(tmp_path / 'reuse.sqlite')
    agent, first = run_agent(IdentityAwareCommerceAgent, store, sid, [
        tool('get_stock', product_id='us:IDENTITY-FIXTURE'), answer()])
    assert 'error' not in first
    agent.client = ScriptedClient([answer()])
    second = asyncio.run(agent.run(sid, 'Report the product without using a tool.'))
    assert second['error'] == 'Final product list includes an unobserved catalog ID'
    assert second['model_calls'] == 1


def test_previous_run_quote_does_not_authorize_a_new_report(tmp_path):
    store, sid = make_store(tmp_path / 'old-quote.sqlite')
    agent, first = run_agent(IdentityAwareCommerceAgent, store, sid, [
        tool('get_stock', product_id='us:IDENTITY-FIXTURE'), answer()])
    assert 'error' not in first
    store.remember_products(sid, [store.catalog.get('us:IDENTITY-FIXTURE')])
    store.change_cart(sid, 'us:IDENTITY-FIXTURE', 1)
    old_quote = store.propose_order(sid, destination='GB', deadline_days=30, shipping_budget_usd=100)
    agent.last_quote = old_quote
    agent.client = ScriptedClient([answer(proposal_id=old_quote['proposal_id'])])
    second = asyncio.run(agent.run(sid, 'Report the current status without staging an order.'))
    assert second['error'] == 'Final proposal ID was not produced by this run'

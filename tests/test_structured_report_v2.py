"""Exercise report provenance through real tools and SQLite state, without an API."""
import asyncio
from contextlib import closing
import json

import pytest

from commerce_lab.skills import BASELINE
from commerce_lab.state import BusinessError, Store, WAREHOUSES
from commerce_lab_v2.structured import StructuredReportAgent


FIRST, SECOND = 'us:REPORT-FIRST', 'us:REPORT-SECOND'


class Catalog:
    def get(self, ident):
        if ident not in {FIRST, SECOND}:
            return None
        return {'id': ident, 'title': 'White shirt' if ident == FIRST else 'Other shirt',
                'brand': 'Fixture brand', 'price_usd': 10.0, 'weight_kg': 0.5,
                'evidence_id': 'fixture:' + ident, 'locale': 'us', 'color': 'Style 2',
                'description': 'Soft cotton-blend fabric. Package quantity is unspecified.'}

    def search(self, query, **kwargs):
        return [self.get(FIRST)]


class Client:
    config = {'COMMERCE_MODEL': 'offline-script'}

    def __init__(self, messages):
        self.messages = iter(messages)

    def chat(self, *args, **kwargs):
        message = next(self.messages)
        if callable(message):
            message = message()
        return {'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop',
                'estimated_cost_cny': '0', 'requested_model': 'offline-script'}


def calls(*entries):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': f'call-{i}', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(entries)]}


def done(ids=None, status='completed', missing=None, proposal=None):
    return {'role': 'assistant', 'content': json.dumps({
        'product_ids': ids if ids is not None else [FIRST], 'status': status,
        'missing_fields': missing or [], 'proposal_id': proposal})}


def setup(tmp_path, stock=10):
    store = Store(Catalog(), tmp_path / 'store.sqlite')
    sid = store.session()['id']
    with closing(store.connect()) as db, db:
        db.executemany('INSERT INTO stock VALUES (?,?,?,1)',
                       [(ident, wh, stock) for ident in (FIRST, SECOND) for wh in WAREHOUSES])
    return store, sid


def run(store, sid, messages, task='Describe this product.', agent=None):
    if agent is None:
        agent = StructuredReportAgent(store=store, client=Client(messages), policy=BASELINE, topology='single')
    else:
        agent.client = Client(messages)
    result = asyncio.run(agent.run(sid, task))
    return agent, result


def test_stock_only_report_does_not_invent_attributes_or_enable_writes(tmp_path):
    store, sid = setup(tmp_path, stock=1)
    _, result = run(store, sid, [calls(('get_stock', {'product_id': FIRST})),
                                 done(status='infeasible')], task='Check availability for 3 catalog units.')
    assert 'error' not in result
    assert result['report_evidence']['details'] == {}
    assert {r['quantity'] for r in result['report_evidence']['stocks'][FIRST]['warehouses']} == {1}
    assert 'Observed synthetic stock' in result['report']['answer']
    assert 'White shirt' not in result['report']['answer']
    with pytest.raises(BusinessError, match='Read this product'):
        store.change_cart(sid, FIRST, 1)


def test_catalog_fields_are_attributed_without_resolving_conflict_or_pack_size(tmp_path):
    store, sid = setup(tmp_path)
    _, result = run(store, sid, [calls(('get_product_details', {'product_id': FIRST})), done()])
    assert 'error' not in result
    answer = result['report']['answer']
    assert 'Title: White shirt' in answer and 'Catalog color field: Style 2' in answer
    assert json.dumps(store.catalog.get(FIRST)['description']) in answer
    assert 'one shirt' not in answer.lower() and 'one item' not in answer.lower()
    assert result['report_evidence']['details'][FIRST]['attributes']['color'] == 'Style 2'
    assert store.run(sid, result['run_id'])['result']['report_evidence'] == result['report_evidence']


def test_infeasible_quote_reports_real_before_after_cart_and_exact_constraints(tmp_path):
    store, sid = setup(tmp_path)
    _, result = run(store, sid, [calls(
        ('get_product_details', {'product_id': FIRST}),
        ('set_cart_item', {'product_id': FIRST, 'quantity': 2}),
        ('quote_shipping', {'destination': 'GB', 'deadline_days': 30, 'shipping_budget_usd': 0.01})),
        done(status='infeasible')], task='Prepare 2 catalog units and quote shipping to GB within 30 days for USD 0.01.')
    assert 'error' not in result
    evidence = result['report_evidence']
    assert evidence['cart_before']['items'] == []
    assert evidence['cart_after']['items'][0]['quantity'] == 2
    assert evidence['quote']['plan']['status'] != 'planned'
    answer = result['report']['answer']
    assert 'start of this run: 0 lines, 0 catalog units' in answer
    assert 'At the end: 1 lines, 2 catalog units' in answer
    assert 'Cart contents changed' in answer and 'unchanged' not in answer
    assert '0.01 shipping budget' in answer
    assert result['report_evidence']['staged_proposal_ids'] == []


def test_equal_counts_with_replaced_sku_are_a_changed_cart(tmp_path):
    store, sid = setup(tmp_path)
    store.remember_products(sid, [store.catalog.get(FIRST)])
    store.change_cart(sid, FIRST, 1)
    _, result = run(store, sid, [calls(
        ('get_product_details', {'product_id': SECOND}),
        ('set_cart_item', {'product_id': FIRST, 'quantity': 0}),
        ('set_cart_item', {'product_id': SECOND, 'quantity': 1})), done(ids=[SECOND])],
        task='Replace the first product with one catalog unit of the second.')
    assert 'error' not in result
    assert 'Cart contents changed' in result['report']['answer']
    assert result['cart']['items'][0]['product_id'] == SECOND


def test_missing_budget_is_a_question_and_leaves_cart_untouched(tmp_path):
    store, sid = setup(tmp_path)
    store.remember_products(sid, [store.catalog.get(FIRST)])
    store.change_cart(sid, FIRST, 2)
    before = store.cart(sid)
    _, result = run(store, sid, [calls(('get_cart', {})),
                                done(status='needs_clarification', missing=['shipping_budget'])],
        task='Quote the current cart to JP within 30 days. Ask if information is missing.')
    assert 'error' not in result
    assert 'Please provide the shipping budget in USD.' in result['report']['answer']
    assert result['report_evidence']['language'] == 'en'
    assert result['report_evidence']['quote'] is None and store.cart(sid) == before


def test_source_rendering_handles_search_results_and_chinese_request(tmp_path):
    store, sid = setup(tmp_path)
    _, result = run(store, sid, [calls(('search_products', {'query': 'shirt'})), done()],
        task='请查询衬衫，不要添加到购物车。')
    assert 'error' not in result
    assert '目录描述原文摘录' in result['report']['answer']
    assert result['report_evidence']['language'] == 'zh'
    assert result['cart']['items'] == []


def test_successful_proposal_comes_from_actual_tool_and_is_not_an_order(tmp_path):
    store, sid = setup(tmp_path)
    agent = StructuredReportAgent(store=store, client=Client([]), policy=BASELINE, topology='single')
    _, result = run(store, sid, [calls(
        ('get_product_details', {'product_id': FIRST}),
        ('set_cart_item', {'product_id': FIRST, 'quantity': 2}),
        ('stage_order', {'destination': 'JP', 'deadline_days': 30, 'shipping_budget_usd': 100})),
        lambda: done(proposal=agent.last_quote['proposal_id'])],
        task='Prepare 2 catalog units for JP within 30 days, shipping budget USD 100; stage only.', agent=agent)
    assert 'error' not in result
    quote = result['quote']
    assert quote['plan']['status'] == 'planned' and quote['audit']['passed']
    assert result['report_evidence']['staged_proposal_ids'] == [quote['proposal_id']]
    assert quote['proposal_id'] in result['report']['answer']
    assert f"shipping USD {quote['plan']['total_cost_usd']:.2f}" in result['report']['answer']
    assert store.orders(sid) == []


@pytest.mark.parametrize('unknown,proposal,expected', [
    (True, None, 'unobserved catalog ID'), (False, 'forged-proposal', 'not produced by this run')])
def test_structured_output_preserves_provenance_rejections(tmp_path, unknown, proposal, expected):
    store, sid = setup(tmp_path)
    ident = 'us:UNKNOWN' if unknown else FIRST
    _, result = run(store, sid, [calls(('get_stock', {'product_id': ident})),
                                done(ids=[ident], proposal=proposal)])
    assert expected in result['error']


def test_service_records_and_product_observations_do_not_leak_to_next_run(tmp_path):
    store, sid = setup(tmp_path)
    agent, first = run(store, sid, [calls(('get_product_details', {'product_id': FIRST}),
                                          ('search_policies', {}), ('get_orders', {})), done()])
    assert 'error' not in first
    assert len(first['report_evidence']['service_records']) == 2
    assert first['report_evidence']['details']
    _, second = run(store, sid, [calls(('get_orders', {})), done(ids=[])], task='List my orders.', agent=agent)
    assert 'error' not in second
    assert second['report_evidence']['details'] == {}
    assert second['report_evidence']['service_records'] == [('get_orders', [])]
    assert second['model_calls'] == 2 and second['estimated_cost_cny'] == '0'


def test_schema_rejects_narrative_and_inconsistent_clarification(tmp_path):
    store, sid = setup(tmp_path)
    message = done(status='completed', missing=['shipping_budget'])
    _, result = run(store, sid, [message])
    assert result['error_type'] == 'ValidationError'
    message = done(ids=[])
    obj = json.loads(message['content'])
    obj['answer'] = 'A model-written unsupported claim.'
    message['content'] = json.dumps(obj)
    _, result = run(store, sid, [message])
    assert result['error_type'] == 'ValidationError'


@pytest.mark.parametrize('tool_ident', [FIRST, FIRST.split(':', 1)[1]])
def test_stock_only_multi_agent_trace_passes_external_strict_replay(tmp_path, tool_ident):
    pytest.importorskip('tau2')
    from evaluation.prepare_cases import make_cases
    from evaluation_v2.run import run_case
    case = next(c for c in make_cases(FIRST, 'development', 0) if c['family'] == 'stock_shortage')
    case.update(id='v2-offline-stock-replay', response_language='en')
    client = Client([calls(('ask_catalog_agent', {'task': 'Check stock for three catalog units, leave cart unchanged if insufficient.'})),
                     calls(('get_stock', {'product_id': tool_ident})),
                     {'role':'assistant','content':'Observed stock is one unit in each warehouse, insufficient for three.'},
                     done(status='infeasible')])
    row = run_case(case, tmp_path, 'structured_multi', client=client, catalog=Catalog())
    assert row['run_status'] == 'completed' and row['score']['passed']
    assert row['score']['live_replay_state_matches']
    assert row['settled_cost_cny'] == '0'
    record = json.loads((tmp_path/case['id']/'actual_run.json').read_text(encoding='utf-8'))
    assert record['result']['report_evidence']['details'] == {}
    if tool_ident != FIRST:
        assert any(t['kind'] == 'tool_argument_normalization' for t in record['traces'])
        raw_responses = [t['payload']['message'] for t in record['traces'] if t['kind'] == 'model_response']
        assert any(json.loads(call['function']['arguments']).get('product_id') == tool_ident
                   for response in raw_responses for call in response.get('tool_calls', []))


@pytest.mark.parametrize('task,tool_ident', [
    ('Check the requested product stock.', 'REPORT-FIRST'),
    ('Compare us:REPORT-FIRST and jp:REPORT-FIRST.', 'REPORT-FIRST'),
    ('Use us:REPORT-FIRST.', 'jp:REPORT-FIRST')])
def test_id_resolution_never_guesses_locale_or_resolves_ambiguous_references(tmp_path, task, tool_ident):
    store,sid=setup(tmp_path)
    _, result=run(store,sid,[calls(('get_stock',{'product_id':tool_ident})),done()],task=task)
    assert 'unobserved catalog ID' in result['error']
    record=store.run(sid,result['run_id'])
    assert not any(t['kind']=='tool_argument_normalization' for t in record['traces'])


def test_id_resolution_is_run_local_and_does_not_authorize_cart_write(tmp_path):
    store,sid=setup(tmp_path)
    agent,first=run(store,sid,[calls(('get_stock',{'product_id':'REPORT-FIRST'})),done(status='infeasible')],
                    task='Check stock of us:REPORT-FIRST, without changing the cart.')
    assert 'error' not in first
    with pytest.raises(BusinessError,match='Read this product'):
        store.change_cart(sid,FIRST,1)
    _,second=run(store,sid,[calls(('get_stock',{'product_id':'REPORT-FIRST'})),done()],
                 task='Check the stock again.',agent=agent)
    assert 'unobserved catalog ID' in second['error']


@pytest.mark.parametrize('identifiers', [[], ['description','description'], ['description','invented']])
def test_communication_audit_rejects_missing_duplicate_or_invented_checks(tmp_path, identifiers):
    from evaluation_v2.communication import audit_communication
    response = {'language_passed': True, 'language_reason':'English',
                'checks':[{'requirement_id':ident,'passed':True,'reason':'Test assertion'} for ident in identifiers]}
    client = Client([calls(('submit_communication',response))])
    with pytest.raises(ValueError):
        audit_communication({'family':'describe_only','response_language':'en','task':'Describe item and give price.'},
                            {'answer':'Fixture item, USD 10.'},raw_path=tmp_path/'audit.json',client=client)

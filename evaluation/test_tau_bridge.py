from pathlib import Path
import pytest

from evaluation.tau_bridge import CommerceReplayTools, evaluate_record
from commerce_lab.agent import FinalReport


class FixtureCatalog:
    def get(self, ident):
        if ident != 'us:FIXTURE':
            return None
        return {'id': ident, 'product_id': 'FIXTURE', 'title': 'Blue cotton shirt',
            'brand': 'Fixture', 'color': 'Blue', 'description': 'Cotton test fixture.',
            'price_usd': 12.34, 'weight_kg': .5, 'locale': 'us', 'evidence_id': 'fixture:' + ident}


def setup_record(tmp_path, actual_quantity):
    case = {'id': 'test', 'product_id': 'us:FIXTURE', 'task': 'Prepare 2 units; report the ID.',
        'gold_actions': [{'name': 'get_product_details', 'arguments': {'product_id': 'us:FIXTURE'}},
            {'name': 'set_cart_item', 'arguments': {'product_id': 'us:FIXTURE', 'quantity': 2}}],
        'communicate_info': ['us:FIXTURE'], 'allowed_statuses': ['completed']}
    live = CommerceReplayTools(case, tmp_path / 'live.sqlite', {}, catalog=FixtureCatalog())
    trace = []
    operations = [('get_product_details', {'product_id': 'us:FIXTURE'}),
        ('set_cart_item', {'product_id': 'us:FIXTURE', 'quantity': 1}),
        ('set_cart_item', {'product_id': 'us:FIXTURE', 'quantity': actual_quantity})]
    for i, (name, arguments) in enumerate(operations):
        trace.append({'id': i, 'kind': 'tool_result', 'payload': {
            'name': name, 'arguments': arguments, 'output': live.perform(name, arguments)}})
    record = {'status': 'completed', 'traces': trace,
        'result': {'report': {'status': 'completed', 'product_ids': ['us:FIXTURE'], 'answer': 'Prepared.'}}}
    return case, record, live.snapshot()


def test_external_scorer_accepts_different_action_sequence_with_same_final_state(tmp_path: Path):
    case, record, snapshot = setup_record(tmp_path, 2)
    score = evaluate_record(case, record, snapshot, tmp_path / 'score', policy={}, catalog=FixtureCatalog())
    assert score['passed']
    assert score['db']['db_check']['db_match']


def test_correct_words_cannot_hide_wrong_actual_cart(tmp_path: Path):
    case, record, snapshot = setup_record(tmp_path, 3)
    score = evaluate_record(case, record, snapshot, tmp_path / 'score', policy={}, catalog=FixtureCatalog())
    assert not score['passed']
    assert not score['db']['db_check']['db_match']
    assert score['communication']['reward'] == 1


def test_unlogged_side_effect_fails_live_replay_gate(tmp_path: Path):
    case, record, snapshot = setup_record(tmp_path, 2)
    snapshot['stock'][0]['quantity'] -= 1
    score = evaluate_record(case, record, snapshot, tmp_path / 'score', policy={}, catalog=FixtureCatalog())
    assert not score['passed']
    assert not score['live_replay_state_matches']


def test_fabricated_tool_result_fails_strict_replay(tmp_path: Path):
    case, record, snapshot = setup_record(tmp_path, 2)
    record['traces'][-1]['payload']['output']['subtotal_usd'] = 0
    with pytest.raises(ValueError):
        evaluate_record(case, record, snapshot, tmp_path / 'score', policy={}, catalog=FixtureCatalog())


def test_stock_provenance_hook_records_real_read_and_external_replay_agrees(tmp_path: Path):
    case = {'id':'hook-fixture','product_id':'us:FIXTURE','task':'Read stock and report the ID.',
        'gold_actions':[],'communicate_info':['us:FIXTURE'],'allowed_statuses':['completed']}
    policy = {'id':'hook-fixture','pre_read_stock':True}
    live = CommerceReplayTools(case,tmp_path/'live.sqlite',policy,catalog=FixtureCatalog())
    arguments = {'product_id':'us:FIXTURE'}
    output = live.perform('get_stock',arguments)
    live.store.trace(live.agent.run_id,'catalog','tool_result',{'name':'get_stock','arguments':arguments,'output':output})
    report = FinalReport(answer='Stock checked for us:FIXTURE.',product_ids=['us:FIXTURE'],status='completed')
    assert live.agent.ground_report(report)['structured_grounding_passed']
    record = live.store.run(live.session_id,live.agent.run_id)
    record['status'] = 'completed'
    record['result'] = {'report':report.model_dump()}
    reads = [t for t in record['traces'] if t['kind']=='tool_result' and t['payload']['name']=='get_product_details']
    assert len(reads)==1
    assert reads[0]['payload']['output']['product_id']=='us:FIXTURE'
    score = evaluate_record(case,record,live.snapshot(),tmp_path/'score',policy=policy,catalog=FixtureCatalog())
    assert score['passed']

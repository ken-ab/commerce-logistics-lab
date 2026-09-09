from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from apparel_fulfillment.agent_reliability_v6 import ReliabilityAgent
from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.reliability_support import revision_comparison, render_revision, completion_guide
from apparel_fulfillment.data import load_world
from apparel_fulfillment.store import ApparelStore
from test_apparel_agent import Client, tool
from test_apparel_source_review import proposal_workspace, SKU, NOW


def finish_from_guide(messages, *, sku=SKU, rationale='Use the observed state and program comparison.'):
    body = json.loads(messages[-1]['content'])
    guide = body['completion_guide']
    context = body['object_context']
    assert not guide['missing_evidence_groups'] and guide['citation_count'] <= 12
    return [tool('finish', status='ready', product_skus=[sku], proposal_id=context['current_proposal_id'],
        question_codes=[], selection_snapshot=context['selected_lines'],
        citations=[g['citation'] for g in guide['required_available']], rationale=rationale)]


def test_bad_delegated_id_uses_exact_scoped_directory_without_automatic_retry(proposal_workspace):
    store, draft, proposal = proposal_workspace
    wrong = proposal['proposal_id'] + '5'
    def expert_read(messages):
        inputs = json.loads(messages[1]['content'])
        assert wrong in inputs['delegated_subtask']
        assert inputs['object_context']['reference_proposal_id'] == proposal['proposal_id']
        return [tool('read_proposal', proposal_id=wrong)]
    def expert_repair(messages):
        obs = json.loads(messages[-1]['content'])
        assert not obs['success'] and obs['object_context']['current_proposal_id'] == proposal['proposal_id']
        return [tool('read_proposal', proposal_id=obs['object_context']['reference_proposal_id']), tool('read_variant', sku=SKU)]
    client = Client([[tool('delegate', expert='logistics', task='Read proposal '+wrong, reason='Review this existing proposal.')],
                     expert_read, expert_repair, finish_from_guide, finish_from_guide])
    result = ReliabilityAgent(store, 'owner', draft['id'], client=client, arm='coordinator', now=NOW,
                              contract={'mode':'review_proposal'}).run('Review the existing proposal and keep it if valid.')
    assert result['run_status'] == 'completed' and result['model_calls'] == 5
    assert result['before'] == result['after']
    assert result['tool_calls'] == 4 and result['successful_tool_calls'] == 3
    assert [t['arguments']['proposal_id'] for t in result['traces'] if t['kind']=='tool' and t['tool']=='read_proposal'] == [wrong, proposal['proposal_id']]


def test_fourteen_citations_are_rejected_then_compact_guide_repairs_without_raising_cap(proposal_workspace):
    store, draft, proposal = proposal_workspace
    def oversized(messages):
        call = finish_from_guide(messages)[0]
        args = json.loads(call['function']['arguments'])
        args['citations'] = (args['citations'] * 14)[:14]
        return [tool('finish', **args)]
    client = Client([[tool('read_proposal', proposal_id=proposal['proposal_id']), tool('read_variant', sku=SKU)],
                     oversized, finish_from_guide])
    result = ReliabilityAgent(store,'owner',draft['id'],client=client,now=NOW,
                              contract={'mode':'review_proposal'}).run('Review the proposal.')
    assert result['run_status']=='completed' and result['model_calls']==3
    assert len(result['report']['decision']['citations']) == 2
    assert any(t['kind']=='tool_rejected' and '14' in t['detail'] for t in result['traces'])
    assert result['before']==result['after']


def test_guide_does_not_silently_read_missing_material_or_waive_checks(proposal_workspace):
    store,draft,proposal=proposal_workspace
    client=Client([[tool('read_proposal',proposal_id=proposal['proposal_id'])]]+[finish_from_guide]*11)
    result=ReliabilityAgent(store,'owner',draft['id'],client=client,now=NOW,
                            contract={'mode':'review_proposal'}).run('Review this proposal.')
    assert result['run_status']=='call_limit' and result['model_calls']==12
    assert not result['source_review']['passed'] and result['before']==result['after']
    assert result['tool_calls']==2


def test_missing_material_description_uses_only_available_observed_title():
    obs={'O-1':{'success':True,'tool':'read_variant','result':{'variant':{'sku':'X','source_record':{'description':None,'title':'Recorded title'}}}}}
    guide=completion_guide(TaskContract(mode='inspect_product',product_sku='X',product_fields=['material']),{},obs,None)
    assert guide['required_available'][0]['citation']['pointer']=='/result/variant/source_record/title'
    obs['O-1']['result']['variant']['source_record']['title']=None
    guide=completion_guide(TaskContract(mode='inspect_product',product_sku='X',product_fields=['material']),{},obs,None)
    assert guide['missing_evidence_groups']==['material'] and not guide['required_available']


def segment(nominal='2028-02-03T16:00:00Z', *, actual=None, events=(), wait=240):
    return {'leg_id':'air','service_id':'air@'+nominal,'nominal_departure':nominal,
        'departure_at':actual or nominal,'arrival_at':'2028-02-06T10:00:00Z',
        'event_ids':list(events),'wait_minutes':wait}


def proposal(ident='P1', version=1, previous=None, seg=None):
    return {'proposal_id':ident,'draft_id':'ORDER-A','version':version,'previous_proposal_id':previous,
        'request_revision':2,'route':{'status':'planned','segments':[segment() if seg is None else seg],
                                    'total_cost_cents':10000,'arrival_at':'2028-02-07T10:00:00Z'}}


def event(ident='E1', delay=2160, **fields):
    return {'event_id':ident,'leg_id':'air','nominal_departure':'2028-02-03T16:00:00Z','kind':'delay',
            'delay_minutes':delay,'published_at':'2028-02-01T00:00:00Z',**fields}


def compare(old, new, events):
    return revision_comparison(old,new,events,'2028-02-02T00:00:00Z')


def test_replacement_flight_is_not_the_delayed_original():
    old=proposal();new=proposal('P2',2,'P1',segment('2028-02-04T04:00:00Z',wait=960))
    output=compare(old,new,[event()]);row=output['segments'][0]
    assert row['change']=='replaced_service'
    assert row['old_service_effect']['departure_after_known_events']=='2028-02-05T04:00:00Z'
    assert row['new_service_effect']['event_ids']==[] and row['new']['departure_at']=='2028-02-04T04:00:00Z'
    explanation=render_revision(output)
    assert '另一班' in explanation and '2028-02-05T04:00:00Z' in explanation and '2028-02-04T04:00:00Z' in explanation


def test_same_service_accumulates_distinct_delays_and_deduplicates_events():
    first,second=event('D1',60),event('D2',120)
    old=proposal();new=proposal('P2',2,'P1',segment(actual='2028-02-03T19:00:00Z',events=['D1','D2'],wait=420))
    output=compare(old,new,[first,second,first]);row=output['segments'][0]
    assert row['change']=='retained_service' and row['new_service_effect']['delay_minutes']==180
    assert row['wait_change_minutes']==180


def test_replacement_may_have_its_own_different_delay():
    next_time='2028-02-04T04:00:00Z'
    replacement=event('NEXT',30,nominal_departure=next_time)
    new=proposal('P2',2,'P1',segment(next_time,actual='2028-02-04T04:30:00Z',events=['NEXT']))
    output=compare(proposal(),new,[event(),replacement])
    assert output['segments'][0]['new_service_effect']['delay_minutes']==30
    assert output['segments'][0]['new_service_effect']['event_ids']==['NEXT']


def test_cancellation_takes_priority_and_no_fictional_departure_is_reported():
    cancel=event('C',0,kind='cancel')
    new=proposal('P2',2,'P1',segment('2028-02-04T04:00:00Z'))
    output=compare(proposal(),new,[event(),cancel])
    assert output['segments'][0]['old_service_effect']['departure_after_known_events'] is None
    assert '原班次 2028-02-03T16:00:00Z 已取消' in render_revision(output)


@pytest.mark.parametrize('extra',[event(published_at='2028-02-03T00:00:00Z'),event(leg_id='sea')])
def test_future_and_other_leg_events_do_not_apply(extra):
    old=proposal();output=compare(old,old,[extra])
    assert output['retained_proposal'] and output['segments'][0]['old_service_effect']['event_ids']==[]


def test_downstream_wait_changes_without_changing_service():
    old=proposal();new=deepcopy(old);new.update(proposal_id='P2',previous_proposal_id='P1',version=2)
    new['route']['segments'][0]['wait_minutes']=60
    output=compare(old,new,[])
    assert output['segments'][0]['change']=='retained_service' and output['segments'][0]['wait_change_minutes']==-180
    assert '240 变为 60 分钟' in render_revision(output)


def test_infeasible_revision_removes_segments_without_claiming_new_flight():
    old=proposal();new=proposal('P2',2,'P1');new['route']={'status':'infeasible','adjustment_options':[]}
    output=compare(old,new,[event()])
    assert output['new_arrival_at'] is None and output['segments'][0]['change']=='removed'
    assert '新版不再包含' in render_revision(output)


@pytest.mark.parametrize('corruption',['order','chain','revision','service','actual','events','naive_time','conflict'])
def test_inconsistent_identity_or_event_facts_are_rejected(corruption):
    old=proposal();new=proposal('P2',2,'P1',segment('2028-02-04T04:00:00Z'));events=[event()]
    if corruption=='order':new['draft_id']='ORDER-B'
    elif corruption=='chain':new['previous_proposal_id']='other'
    elif corruption=='revision':new['request_revision']=9
    elif corruption=='service':new['route']['segments'][0]['service_id']='air@wrong'
    elif corruption=='actual':new['route']['segments'][0]['departure_at']='2028-02-04T08:00:00Z'
    elif corruption=='events':new['route']['segments'][0]['event_ids']=['E1']
    elif corruption=='naive_time':events[0]['published_at']='2028-02-01T00:00:00'
    else:events.append(event(delay=120))
    with pytest.raises(ValueError):compare(old,new,events)


def test_real_store_revision_renders_program_facts_while_preserving_unverified_rationale(tmp_path):
    world=load_world();world['stock'][SKU]['available_catalog_units']=120
    store=ApparelStore(tmp_path/'revision.sqlite',world=world)
    now=datetime(2028,3,1,tzinfo=timezone.utc)
    draft=store.create_draft('owner',{'sales_region':'DE','wholesale':True,'needs_shipping':True,
        'shipping':{'destination':'DE-DC','budget_cents':30000,'ready_at':'2028-03-02T00:00:00Z','deadline_at':'2028-03-10T00:00:00Z'},
        'lines':[{'line_id':'item','quantity':20,'unit':'piece','requested_sku':SKU,'category':'t_shirt'}]})
    draft=store.select('owner',draft['id'],[{'line_id':'item','sku':SKU}],expected_revision=draft['revision'])
    old=store.propose('owner',draft['id'],expected_revision=draft['revision'],now=now)
    air=next(s for s in old['route']['segments'] if s['mode']=='air')
    store.add_transport_event({'event_id':'LIVE-D','kind':'delay','delay_minutes':2160,'leg_id':air['leg_id'],
        'nominal_departure':air['nominal_departure'],'published_at':'2028-03-01T00:00:00Z'})
    def finish(messages):return finish_from_guide(messages,rationale='A deliberately wrong free-text explanation for the regression test.')
    client=Client([[tool('read_proposal',proposal_id=old['proposal_id']),tool('read_variant',sku=SKU),tool('read_transport_events')],
                   [tool('prepare_proposal',expected_revision=draft['revision'])],finish])
    result=ReliabilityAgent(store,'owner',draft['id'],client=client,now=now,
                            contract={'mode':'review_proposal'}).run('Review and revise under the same constraints.')
    assert result['run_status']=='completed'
    report=result['report']
    assert 'deliberately wrong' in report['decision']['rationale']
    assert 'deliberately wrong' not in report['answer'] and '另一班' in report['revision_explanation']
    assert report['revision_comparison']['old_proposal_id']==old['proposal_id']
    assert result['after']['confirmation'] is None and result['after']['request']==result['before']['request']

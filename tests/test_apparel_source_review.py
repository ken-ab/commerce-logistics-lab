from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.agent import MAX_MODEL_CALLS
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.data import digest, load_world
from apparel_fulfillment.source_review import full_material, review_sources
from apparel_fulfillment.store import ApparelStore
from test_apparel_agent import Client, tool

SKU = 'us:B06XWMKR2F'
NOW = datetime(2027, 5, 1, tzinfo=timezone.utc)


@pytest.fixture
def proposal_workspace(tmp_path):
    world = load_world()
    world['stock'][SKU]['available_catalog_units'] = 120
    store = ApparelStore(tmp_path/'source.sqlite', world=world)
    request = {'sales_region':'DE','wholesale':True,'needs_shipping':False,
        'lines':[{'line_id':'item','quantity':20,'unit':'piece','brand':'Goodthreads','color':'black','size':'M','category':'t_shirt'}]}
    draft = store.create_draft('owner',request)
    draft = store.select('owner',draft['id'],[{'line_id':'item','sku':SKU}],expected_revision=draft['revision'])
    proposal = store.propose('owner',draft['id'],expected_revision=draft['revision'],now=NOW)
    return store,draft,proposal


def finish(proposal, *, validity='O-2'):
    return [tool('finish',status='ready',product_skus=[SKU],proposal_id=proposal['proposal_id'],
        question_codes=[],selection_snapshot=[{'line_id':'item','sku':SKU}],
        citations=[{'observation_id':'O-1','pointer':'/result/order_check/status'},
                   {'observation_id':validity,'pointer':'/result/validity/valid'}],rationale='Keep the valid pending proposal.')]


def observed(material, *, tool_name='read_variant', success=True):
    return {'O-2':{'success':success,'tool':tool_name,'result':deepcopy(material)}}


def test_missing_source_rejects_finish_then_model_reads_and_repairs(proposal_workspace):
    store,draft,proposal=proposal_workspace
    before=store.view('owner',draft['id'])
    def open_proposal(messages):
        first=json.loads(messages[1]['content'])['initial_order_observation']
        assert first['source_review']['missing']==[{'sku':SKU,'reason':'complete_current_material_not_observed'}]
        return [tool('read_proposal',proposal_id=proposal['proposal_id'])]
    def repair(messages):
        rejection=json.loads(messages[-1]['content'])
        assert any(e['reason']=='current_product_sources_not_read' for e in rejection['invalid'])
        return [tool('read_variant',sku=SKU)]
    client=Client([open_proposal,finish(proposal),repair,finish(proposal)])
    result=SourceReviewAgent(store,'owner',draft['id'],client=client,now=NOW,
        contract={'mode':'review_proposal'}).run('Read product sources and review this proposal, keeping it if valid.')
    assert result['run_status']=='completed' and result['model_calls']==4
    assert result['tool_calls']==3 and result['source_review']['passed']
    assert result['report']['operation_check']['passed']
    assert result['source_review']['receipts'][0]['observation_id']=='O-3'
    assert result['before']==before==result['after']
    assert len([t for t in result['traces'] if t['kind']=='report_rejected'])==1


def test_unchanged_full_read_is_reusable_for_two_lines_and_no_duplicate_read(proposal_workspace):
    store,draft,_=proposal_workspace
    agent=SourceReviewAgent(store,'owner',draft['id'],client=object(),now=NOW,contract={'mode':'prepare_proposal'})
    agent.observed('read_variant',{'sku':SKU},'product')
    count=agent.tool_calls
    current=agent.view()
    current['selections'].append({'line_id':'second','sku':SKU})
    status=review_sources(agent.contract,current,agent.observations,agent.world())
    assert status['passed'] and status['required_skus']==[SKU] and len(status['receipts'])==1
    assert agent.source_status()['passed'] and agent.tool_calls==count==1


def test_stock_change_invalidates_previous_read_and_fresh_read_repairs(proposal_workspace):
    store,draft,_=proposal_workspace
    agent=SourceReviewAgent(store,'owner',draft['id'],client=object(),now=NOW,contract={'mode':'prepare_proposal'})
    agent.observed('read_variant',{'sku':SKU},'single')
    assert agent.source_status()['passed']
    store.set_simulated_stock(SKU,119,expected_version=1)
    assert not agent.source_status()['passed']
    agent.observed('read_variant',{'sku':SKU},'single')
    receipt=agent.source_status()['receipts'][0]
    assert receipt['observation_id']=='O-2' and receipt['stock_version']==2


@pytest.mark.parametrize('changed',['source','rule','units','provenance'])
def test_changed_material_invalidates_old_evidence_even_without_version_bump(changed):
    world=load_world(); current={'selections':[{'line_id':'item','sku':SKU}]}
    contract=TaskContract(mode='prepare_proposal')
    observations=observed(full_material(world,SKU))
    variant=world['variants'][SKU]
    if changed=='source':
        variant['source_record']['description']+=' Updated source sentence.'
        variant['source_record_sha256']=digest(variant['source_record'])
    elif changed=='rule':world['brand_rules'][variant['brand']]['wholesale_minimum_pieces_per_sku']+=1
    elif changed=='units':variant['pieces_per_catalog_unit']+=1
    else:variant['provenance']['size']['evidence_id']='changed-public-record'
    assert not review_sources(contract,current,observations,world)['passed']


@pytest.mark.parametrize('kind',['failed','wrong_tool','truncated','corrupted_hash','wrong_sku'])
def test_partial_or_unrelated_material_cannot_satisfy_source_requirement(kind):
    world=load_world(); current={'selections':[{'line_id':'item','sku':SKU}]}
    material=full_material(world,SKU)
    if kind=='truncated': material['variant']['description_excerpt_truncated']=True
    if kind=='corrupted_hash':material['variant']['source_record_sha256']='invalid'
    if kind=='wrong_sku':material=full_material(world,'us:B06XWGZD1C')
    observations=observed(material,success=kind!='failed',tool_name='read_order' if kind=='wrong_tool' else 'read_variant')
    assert not review_sources(TaskContract(mode='prepare_proposal'),current,observations,world)['passed']


def test_read_tool_returns_complete_record_and_keeps_hash_consistent(tmp_path):
    world=load_world(); world['variants'][SKU]['source_record']['description']='Original source. '*160
    world['variants'][SKU]['source_record_sha256']=digest(world['variants'][SKU]['source_record'])
    store=ApparelStore(tmp_path/'long-source.sqlite',world=world)
    draft=store.create_draft('owner',{'sales_region':'DE','wholesale':False,'needs_shipping':False,
        'lines':[{'line_id':'item','quantity':1,'unit':'piece','category':'t_shirt'}]})
    agent=SourceReviewAgent(store,'owner',draft['id'],client=object(),contract={'mode':'inspect_product','product_sku':SKU,'product_fields':['material']})
    result=agent.observed('read_variant',{'sku':SKU},'single')
    assert result['success'] and len(result['result']['variant']['source_record']['description'])>1200
    assert digest(result['result']['variant']['source_record'])==result['result']['variant']['source_record_sha256']
    assert agent.source_status()['passed'] and agent.view()['selections']==[]


def test_actual_approved_replacement_is_required_not_original_sku(proposal_workspace):
    store,draft,_=proposal_workspace
    replacement='us:B06XWGZD1C'
    selected=store.select('owner',draft['id'],[{'line_id':'item','sku':replacement}],expected_revision=draft['revision'])
    store.approve_substitution('owner',draft['id'],selected['order_check']['substitution_proposals'][0]['approval_id'],expected_revision=selected['revision'])
    agent=SourceReviewAgent(store,'owner',draft['id'],client=object(),contract={'mode':'prepare_proposal'})
    agent.observed('read_variant',{'sku':SKU},'single')
    assert agent.source_status()['required_skus']==[replacement] and not agent.source_status()['passed']
    agent.observed('read_variant',{'sku':replacement},'single')
    assert agent.source_status()['passed'] and len(agent.view()['approved_substitutions'])==1


def test_repeated_omission_hits_shared_cap_without_silent_read(proposal_workspace):
    store,draft,proposal=proposal_workspace
    client=Client([[tool('read_proposal',proposal_id=proposal['proposal_id'])]]+[finish(proposal)]*(MAX_MODEL_CALLS-1))
    result=SourceReviewAgent(store,'owner',draft['id'],client=client,now=NOW,contract={'mode':'review_proposal'}).run('Review the selected product and proposal.')
    assert result['run_status']=='call_limit' and result['model_calls']==MAX_MODEL_CALLS
    assert not result['source_review']['passed'] and result['before']==result['after']
    assert not any(t.get('tool')=='read_variant' for t in result['traces'])


def test_read_only_order_check_does_not_impose_unrequested_full_source_read(proposal_workspace):
    store,draft,_=proposal_workspace
    agent=SourceReviewAgent(store,'owner',draft['id'],client=object(),contract={'mode':'check_order'})
    assert agent.source_status()['status']=='not_required'
    with pytest.raises(ValueError,match='read-only'):
        agent.invoke('select_variants',{'selections':[],'expected_revision':draft['revision']})

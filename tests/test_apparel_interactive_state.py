from copy import deepcopy
import json

import pytest

import apparel_fulfillment.jobs as job_module
from apparel_fulfillment.interactive_state import InteractiveOperationAgent, UI_POLICY, checked_operation
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.store import ApparelStore
from test_apparel_agent import Client, tool
from test_apparel_state_v3 import finish
from test_apparel_orders import fixture, request
from test_apparel_api import workspace as api_workspace, selected


def test_precheck_rejects_unknown_targets_without_constructing_paid_client(tmp_path, monkeypatch):
    monkeypatch.setattr(job_module, 'ROOT', tmp_path)
    store=ApparelStore(tmp_path/'orders.sqlite',world=fixture())
    draft=store.create_draft('owner',request())
    def forbidden_client(): raise AssertionError('Invalid operation must not construct a model client')
    jobs=job_module.AgentJobs(store,directory=tmp_path/'jobs',client_factory=forbidden_client)
    try:
        for operation in ({'mode':'inspect_product','product_sku':'missing','product_fields':['size']},
                          {'mode':'stage_candidate','line_id':'missing'}, {'mode':'review_proposal'},
                          {'mode':'check_order','product_sku':'A'}):
            with pytest.raises(OrderError): jobs.start('owner',draft['id'],'Check only','single',operation=operation)
        with pytest.raises(OrderError): jobs.start('other',draft['id'],'Check only','single',operation={'mode':'check_order'})
        assert jobs.active is None and not list((tmp_path/'jobs').glob('*.json'))
    finally:
        jobs.pool.shutdown(wait=True)


def test_new_interactive_path_enforces_read_only_with_actual_state_agent(tmp_path,monkeypatch):
    monkeypatch.setattr(job_module,'ROOT',tmp_path)
    world=fixture()
    for variant in world['variants'].values(): variant['provenance']['weight_grams_per_catalog_unit']={'evidence_id':'fixture-weight'}
    store=ApparelStore(tmp_path/'orders.sqlite',world=world)
    draft=store.create_draft('owner',request())
    model=Client([[tool('read_variant',sku='A'),tool('select_variants',selections=[{'line_id':'one','sku':'A'}],expected_revision=1)],
                  finish(status='information',observation='O-2',pointer='/result/variant/size')])
    model.ensure_available=lambda model: None
    jobs=job_module.AgentJobs(store,directory=tmp_path/'jobs',client_factory=lambda:model)
    operation={'mode':'inspect_product','product_sku':'A','product_fields':['size']}
    job=jobs.start('owner',draft['id'],'只查尺码，不修改订单','single',operation=operation)
    jobs.pool.shutdown(wait=True)
    record=jobs.get('owner',job['job_id'],full=True);result=record['result']
    assert result['run_status']=='completed' and result['policy_version']==UI_POLICY
    assert result['base_policy_version']=='apparel-operation-state-v3'
    assert result['interventions']=={'bootstrap':True,'enforce_contract':True}
    assert result['observations']['O-3']['success'] is False
    assert result['before']==result['after'] and result['after']['selections']==[]
    assert result['report']['operation_check']['passed']
    assert record['operation']['product_fields']==['size']
    assert jobs.get('owner',job['job_id'])['operation']==record['operation']


def test_api_validates_operation_and_passes_explicit_contract(api_workspace):
    client,store,sessions=api_workspace
    draft,path=selected(client)
    class Jobs:
        calls=[]
        def start(self,owner,draft_id,task,arm,*,operation=None):
            self.calls.append((owner,draft_id,task,arm,operation))
            return {'status':'recorded_for_test','operation':operation}
    jobs=Jobs()
    # The installed route closes over its own jobs; patch that instance's start,
    # so routing/session/Pydantic validation remain real and no network is used.
    real=client.app.state.apparel_jobs
    original=real.start
    real.start=jobs.start
    try:
        assert client.post(path+'/agent',json={'task':'Check','arm':'single','operation':{'mode':'inspect_product'}}).status_code==422
        assert client.post(path+'/agent',json={'task':'Check','arm':'single','operation':{'mode':'check_order','approved':True}}).status_code==422
        response=client.post(path+'/agent',json={'task':'Check','arm':'single','operation':{'mode':'check_order'}})
        assert response.status_code==200 and response.json()['operation']['mode']=='check_order'
        response=client.post(path+'/agent',json={'task':'Legacy flow','arm':'single'})
        assert response.json()['operation'] is None
        assert len(jobs.calls)==2
    finally:
        real.start=original


def test_review_keeps_proposal_identity_and_rejects_another_orders_id(tmp_path):
    store=ApparelStore(tmp_path/'orders.sqlite',world=fixture())
    drafts=[]
    for _ in range(2):
        d=store.create_draft('owner',request())
        d=store.select('owner',d['id'],[{'line_id':'one','sku':'A'}],expected_revision=d['revision'])
        p=store.propose('owner',d['id'],expected_revision=d['revision'])
        drafts.append((d,p))
    first,other=drafts
    op=checked_operation(store,'owner',first[0]['id'],{'mode':'review_proposal','proposal_id':first[1]['proposal_id']})
    assert op['proposal_id']==first[1]['proposal_id']
    with pytest.raises(OrderError): checked_operation(store,'owner',first[0]['id'],{'mode':'review_proposal','proposal_id':other[1]['proposal_id']})


@pytest.mark.parametrize('mode', ['prepare_proposal', 'review_proposal'])
def test_proposal_operations_cannot_replace_an_approved_selection(tmp_path, mode):
    world=fixture()
    for v in world['variants'].values(): v['provenance']['weight_grams_per_catalog_unit']={'evidence_id':'fixture-weight'}
    world['stock']['A']['available_catalog_units']=0
    store=ApparelStore(tmp_path/'approved.sqlite',world=world)
    draft=store.create_draft('owner',request())
    draft=store.select('owner',draft['id'],[{'line_id':'one','sku':'B'}],expected_revision=draft['revision'])
    approval=draft['order_check']['substitution_proposals'][0]['approval_id']
    draft=store.approve_substitution('owner',draft['id'],approval,expected_revision=draft['revision'])
    model=Client([])
    agent=InteractiveOperationAgent(store,'owner',draft['id'],client=model,contract={'mode':mode})
    before=agent.view()
    rejected=agent.observed('select_variants',{'selections':[{'line_id':'one','sku':'A'}],
                                             'expected_revision':draft['revision']},'single')
    assert not rejected['success'] and 'preserves current selections' in rejected['result']['error']
    assert agent.view()==before and not model.inputs


def test_preparation_recovers_from_rejected_reselection_without_losing_approval(tmp_path,monkeypatch):
    monkeypatch.setattr(job_module,'ROOT',tmp_path)
    world=fixture()
    for v in world['variants'].values(): v['provenance']['weight_grams_per_catalog_unit']={'evidence_id':'fixture-weight'}
    world['stock']['A']['available_catalog_units']=0
    store=ApparelStore(tmp_path/'recovery.sqlite',world=world)
    draft=store.create_draft('owner',request())
    draft=store.select('owner',draft['id'],[{'line_id':'one','sku':'B'}],expected_revision=draft['revision'])
    draft=store.approve_substitution('owner',draft['id'],draft['order_check']['substitution_proposals'][0]['approval_id'],expected_revision=draft['revision'])
    def prepare_after_rejection(messages):
        assert not json.loads(messages[-1]['content'])['success']
        return [tool('prepare_proposal',expected_revision=draft['revision'])]
    def report_proposal(messages):
        obs=json.loads(messages[-1]['content'])
        return finish(proposal=obs['result']['proposal_id'],observation=obs['observation_id'],pointer='/result/order_check',products=['B'])
    client=Client([[tool('select_variants',selections=[{'line_id':'one','sku':'A'}],expected_revision=draft['revision'])],
                   prepare_after_rejection,report_proposal])
    client.ensure_available=lambda model:None
    jobs=job_module.AgentJobs(store,directory=tmp_path/'jobs',client_factory=lambda:client)
    job=jobs.start('owner',draft['id'],'Prepare current approved order','single',operation={'mode':'prepare_proposal'})
    jobs.pool.shutdown(wait=True)
    result=jobs.get('owner',job['job_id'],full=True)['result']
    assert result['run_status']=='completed' and result['report']['operation_check']['passed']
    assert result['after']['selections']==result['before']['selections']==[{'line_id':'one','sku':'B'}]
    assert result['after']['approved_substitutions']==result['before']['approved_substitutions']
    assert result['after']['confirmation'] is None and len(result['after']['proposals'])==1
    assert result['model_calls']==3 and result['tool_calls']==3


def test_latest_result_endpoint_restores_completed_job_with_order_and_owner_isolation(api_workspace):
    client,store,sessions=api_workspace
    draft,path=selected(client)
    latest_path=path+'/agent/latest'
    assert client.get(latest_path).json() is None
    jobs=client.app.state.apparel_jobs
    for digit,created in [('1','2026-09-08T09:00:00+00:00'),('2','2026-09-08T10:00:00+00:00')]:
        jobs.save({'job_id':'AJOB-'+digit*32,'owner':draft['owner'],'draft_id':draft['id'],
                   'arm':'single','operation':{'mode':'check_order'},'status':'completed',
                   'created_at':created,'result':None})
    # A later foreign record with the same draft string must never leak.
    jobs.save({'job_id':'AJOB-'+'3'*32,'owner':'another-owner','draft_id':draft['id'],
               'arm':'single','operation':None,'status':'completed','created_at':'2026-09-08T11:00:00+00:00','result':None})
    response=client.get(latest_path)
    assert response.status_code==200 and response.json()['job_id']=='AJOB-'+'2'*32
    assert response.json()['status']=='completed'
    other=client.post('/api/apparel/drafts',json={'order':request()}).json()
    assert client.get('/api/apparel/drafts/'+other['id']+'/agent/latest').json() is None
    with pytest.raises(OrderError): jobs.latest('another-owner',draft['id'])

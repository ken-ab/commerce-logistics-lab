import pytest

import apparel_fulfillment.jobs as job_module
from apparel_fulfillment.interactive_sources import InteractiveSourceOperationAgent, UI_POLICY
from test_apparel_agent import Client, tool
from test_apparel_source_review import proposal_workspace, finish, NOW, SKU


@pytest.mark.parametrize('operation',['prepare_proposal','review_proposal'])
def test_interactive_source_path_preserves_existing_selection(operation,proposal_workspace):
    store,draft,_=proposal_workspace
    agent=InteractiveSourceOperationAgent(store,'owner',draft['id'],client=object(),now=NOW,contract={'mode':operation})
    before=store.view('owner',draft['id'])
    with pytest.raises(ValueError,match='preserves current selections'):
        agent.invoke('select_variants',{'selections':[],'expected_revision':draft['revision']})
    obs=agent.observed('read_variant',{'sku':SKU},'single')
    assert obs['success'] and agent.source_status()['passed']
    assert agent.view()==before


def test_persisted_job_keeps_source_receipt_and_separate_confirmation(proposal_workspace,tmp_path,monkeypatch):
    store,draft,proposal=proposal_workspace
    monkeypatch.setattr(job_module,'ROOT',tmp_path)
    client=Client([[tool('read_proposal',proposal_id=proposal['proposal_id'])],
        [tool('read_variant',sku=SKU)],finish(proposal)])
    client.ensure_available=lambda _:None
    jobs=job_module.AgentJobs(store,directory=tmp_path/'jobs',state_agent_factory=InteractiveSourceOperationAgent,client_factory=lambda:client)
    job=jobs.start('owner',draft['id'],'Read selected product sources and retain the valid proposal.','single',operation={'mode':'review_proposal'})
    jobs.pool.shutdown(wait=True)
    record=jobs.get('owner',job['job_id'],full=True);result=record['result']
    assert record['status']=='completed' and result['policy_version']==UI_POLICY
    assert result['report']['source_review']['passed'] and result['report']['operation_check']['passed']
    assert result['after']['confirmation'] is None and result['before']==result['after']
    assert jobs.get('owner',job['job_id'])['result']['report']['source_review']['passed']


def test_coordinator_reuses_product_expert_read_at_root_finish(proposal_workspace):
    store,draft,proposal=proposal_workspace
    client=Client([
        [tool('delegate',expert='product',task='Read current selected product material.',reason='Check product sources')],
        [tool('read_variant',sku=SKU)],
        [tool('finish',status='information',product_skus=[SKU],proposal_id=None,question_codes=[],selection_snapshot=None,
              citations=[{'observation_id':'O-2','pointer':'/result/variant/brand'}],rationale='Product material was read.')],
        [tool('delegate',expert='logistics',task='Read current proposal validity.',reason='Check current fulfillment state')],
        [tool('read_proposal',proposal_id=proposal['proposal_id'])],
        finish(proposal,validity='O-3'),
        finish(proposal,validity='O-3'),
    ])
    result=InteractiveSourceOperationAgent(store,'owner',draft['id'],client=client,arm='coordinator',now=NOW,
        contract={'mode':'review_proposal'}).run('Check product material and retain the valid current proposal.')
    assert result['run_status']=='completed' and result['delegations']==2
    assert result['source_review']['receipts'][0]['observation_id']=='O-2'
    reads=[t for t in result['traces'] if t['kind']=='tool' and t['tool']=='read_variant']
    assert len(reads)==1 and reads[0]['role']=='product'
    assert result['after']==result['before']

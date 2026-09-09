import pytest

import apparel_fulfillment.jobs as job_module
from apparel_fulfillment.agent import ApparelAgent
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.store import ApparelStore
from research.provider_gate import GuardedChatClient
from test_apparel_orders import fixture, request


def test_default_interactive_jobs_use_guard_and_block_before_new_job(tmp_path, monkeypatch):
    monkeypatch.setattr(job_module, 'ROOT', tmp_path)
    class Client:
        card = {'models': {'gpt-5.6-luna': {'provider': 'aihubmix'}}}
        sends = 0
        def chat(self, *args, **kwargs):
            self.sends += 1
            raise AssertionError('No real model calls in this test')
    client = Client()
    monkeypatch.setattr(job_module, 'guarded_business_client', lambda gate: GuardedChatClient(client, gate))
    store = ApparelStore(tmp_path/'orders.sqlite', world=fixture())
    draft = store.create_draft('owner', request())
    jobs = job_module.AgentJobs(store, directory=tmp_path/'jobs')
    try:
        jobs.gate.hold('aihubmix', 'account_balance_insufficient')
        with pytest.raises(OrderError, match='没有发送模型请求'):
            jobs.start('owner', draft['id'], 'Read current order', 'single')
        assert not list((tmp_path/'jobs').glob('*.json')) and jobs.active is None and client.sends==0
        jobs.gate.acknowledge_recovery('aihubmix')
        def run(self, task):
            assert isinstance(self.client, GuardedChatClient)
            self.client.gate.hold('aihubmix', 'payment_required')
            return {'run_status':'failed', 'calls':[], 'report':None}
        monkeypatch.setattr(ApparelAgent, 'run', run)
        job = jobs.start('owner', draft['id'], 'Read current order', 'single')
    finally:
        jobs.pool.shutdown(wait=True)
    record=jobs.get('owner',job['job_id'],full=True)
    assert record['status']=='failed' and 'AIHubMix' in record['notice'] and jobs.active is None
    assert client.sends==0

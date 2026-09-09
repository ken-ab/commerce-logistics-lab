"""Exercise the release entry point through the real HTTP job adapter, offline."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apparel_fulfillment.jobs as job_module
import delivery_apparel_sources as delivery
from apparel_fulfillment.api import install_apparel
from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.interactive_sources import UI_POLICY
from test_apparel_agent import Client, tool
from test_apparel_source_review import proposal_workspace, finish, SKU


def test_http_job_uses_source_policy_and_keeps_existing_proposal(proposal_workspace, tmp_path, monkeypatch):
    store, draft, proposal = proposal_workspace
    before = store.view('owner', draft['id'])
    model = Client([[tool('read_proposal', proposal_id=proposal['proposal_id'])],
                    [tool('read_variant', sku=SKU)], finish(proposal)])
    model.ensure_available = lambda _: None
    monkeypatch.setattr(job_module, 'ROOT', tmp_path)
    jobs = AgentJobs(store, directory=tmp_path / 'jobs', client_factory=lambda: model)

    class Sessions:
        def session(self, owner):
            assert owner == 'owner'

    monkeypatch.setattr(delivery, 'create_previous_app', lambda: install_apparel(
        FastAPI(), operations=store, sessions=Sessions(), jobs=jobs))
    with TestClient(delivery.create_app()) as client:
        client.headers['X-Session-ID'] = 'owner'
        assert client.get('/api/apparel/source-policy').json()['policy_version'] == UI_POLICY
        assert '/apparel-source-research-report' in client.get('/').text
        report = client.get('/apparel-source-research-report')
        assert report.status_code == 200 and '24/24' in report.text
        response = client.post(f'/api/apparel/drafts/{draft["id"]}/agent', json={
            'task': 'Read current product sources and retain the valid proposal.', 'arm': 'single',
            'operation': {'mode': 'review_proposal'}})
        assert response.status_code == 200
        jobs.pool.shutdown(wait=True)
        run = client.get('/api/apparel/runs/' + response.json()['job_id'] + '/records').json()
        assert run['status'] == 'completed' and run['result']['policy_version'] == UI_POLICY
        assert run['result']['report']['source_review']['passed']
        assert run['result']['report']['source_review']['required_skus'] == [SKU]
        assert run['result']['report']['operation_check']['passed']
        assert store.view('owner', draft['id']) == before
        assert before['confirmation'] is None
        assert client.get('/api/apparel/source-policy').json()['active_job_id'] is None

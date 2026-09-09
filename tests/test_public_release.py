"""Distribution integration checks: no paid requests, no GPU, isolated order state."""
import hashlib
import json
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
import pytest

from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.store import ApparelStore
from commerce_lab.state import Store
from public_release.evidence import contained, unpack, ROOT
from public_release.app import create_app
from research.budget import BudgetLedger
from test_apparel_orders import fixture, request
from research.provider_gate import account_failure


def test_archive_rejects_traversal_and_preserves_changed_local_evidence(tmp_path):
    for name in ('../outside', 'C:/outside', '/absolute', 'a\\b'):
        with pytest.raises(ValueError):
            contained(name, tmp_path)
    path=tmp_path/'records.zip'
    data=b'{"record":1}'
    name='evidence/result.json'
    with zipfile.ZipFile(path,'w') as z:z.writestr(name,data)
    manifest={'archives':[{'path':'records.zip','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                         'members':{name:{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}]}
    (tmp_path/'public_release').mkdir()
    (tmp_path/'public_release/archives.json').write_text(json.dumps(manifest),encoding='utf-8')
    assert unpack(tmp_path)['newly_extracted_files']==1
    assert unpack(tmp_path)['newly_extracted_files']==0
    (tmp_path/name).write_text('local changed record',encoding='utf-8')
    with pytest.raises(FileExistsError):unpack(tmp_path)
    assert (tmp_path/name).read_text()=='local changed record'


def test_default_configuration_cannot_reserve_a_paid_call(tmp_path):
    ledger=BudgetLedger(tmp_path/'budget.sqlite',ROOT/'delivery_budget_policy.example.json')
    with pytest.raises(RuntimeError):
        ledger.reserve(maximum_cny='0.01',purpose='commerce_public_test',model='gpt-5.6-luna',price_version='test')
    with ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0


def test_explicit_dashscope_account_rejection_holds_further_requests():
    assert account_failure('HTTPError HTTP 400: Access denied; https://help.aliyun.com/zh/model-studio/error-code#overdue-payment')=='payment_required'
    assert account_failure('HTTPError HTTP 400: invalid response schema') is None


def test_portable_app_routes_owner_and_missing_catalogue(tmp_path):
    world=fixture();world['notice']='Isolated simulation'
    operations=ApparelStore(tmp_path/'apparel.sqlite',world=world)
    class MissingCatalogue:
        path=tmp_path/'not-installed.sqlite'
    sessions=Store(catalog=MissingCatalogue(),path=tmp_path/'sessions.sqlite')
    jobs=AgentJobs(operations,directory=tmp_path/'jobs')
    app=create_app(store=sessions,operations=operations,jobs=jobs,testing=True)
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        assert client.get('/apparel.js').status_code==200
        assert client.get('/apparel-reliability.js').status_code==200
        status=client.get('/api/status').json()
        assert not status['paid_calls_enabled'] and status['budget_limit_cny']==0
        assert not status['catalog']['locally_available'] and status['catalog']['products']==0
        assert client.get('/api/apparel/research').status_code==422
        identity=client.post('/api/sessions').json()['id']
        client.headers['X-Session-ID']=identity
        assert client.get('/api/search',params={'q':'black shirt'}).status_code==503
        assert client.get('/api/apparel/catalog').json()['dataset_id']=='isolated-test-fixture'
        assert client.get('/api/apparel/research').json()['runs_completed']==324
        assert client.get('/api/apparel/reliability-policy').json()['explicit_operations']==['review_proposal']
        draft=client.post('/api/apparel/drafts',json={'order':request()}).json()
        selected=client.post('/api/apparel/drafts/'+draft['id']+'/select',json={
            'selections':[{'line_id':'one','sku':'A'}],'expected_revision':draft['revision']}).json()
        assert selected['order_check']['status']=='ready'
        assert client.get('/api/apparel/drafts/'+draft['id'],headers={'X-Session-ID':sessions.session()['id']}).status_code==409
    jobs.pool.shutdown(wait=True)

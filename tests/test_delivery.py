"""Activation/configuration safeguards; no paid model calls and no live state changes."""
import json

from fastapi.testclient import TestClient
import pytest

import delivery
from commerce_lab.state import Store
from commerce_lab_v2.structured import StructuredReportAgent
from evaluation.business_metrics import summarize
from test_structured_report_v2 import Catalog, Client, done


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding='utf-8')


def fixture_evidence(root):
    ids=[f'fixture-{i}' for i in range(160)]
    frozen={'case_ids':ids,'arms':['identity_multi','structured_multi']}
    fp=root/'evidence/v2_final_freeze.json'; save(fp,frozen)
    directory=root/'evidence/final-fixture'
    save(root/'evidence/v2_test_registration.json',{'status':'complete','directory':str(directory),'freeze_sha256':delivery.sha(fp)})
    arms={}
    for arm in frozen['arms']:
        folder=directory/arm
        rows=[{'case_id':i,'family':'fixture','score':{'passed':True},'run_status':'completed',
               'latency_seconds':1,'settled_cost_cny':'0','model_calls':0} for i in ids]
        save(folder/'results.json',rows); arms[arm]=summarize(rows)
        save(folder/'config.json',{'model':delivery.MODEL,'case_ids':ids,'freeze_sha256':delivery.sha(fp)})
        audit=folder/'audit'
        audit_rows=[{'id':i,'facts':{'decision':{'verdict':'supported'}},'communication':{'passed':True}} for i in ids]
        save(audit/'results.json',audit_rows); save(audit/'inputs.json',[])
        save(audit/'summary.json',{'source_results_sha256':delivery.sha(folder/'results.json'),'scheduled':160,
             'judge_model':'qwen3.8-max','facts_version':delivery.FACT_VERSION,
             'communication':delivery.communication_signature(),'facts_supported':160,'communication_passed':160})
        save(folder/'audit_registration.json',{'status':'complete','directory':str(audit),'summary_sha256':delivery.sha(audit/'summary.json')})
    save(directory/'summary.json',{'status':'complete','partition':'test','arms':arms})
    def load():
        return delivery.release_evidence(root,freeze_validator=lambda path:frozen,input_builder=lambda folder:([],{}))
    return directory,load


def test_no_final_registration_cannot_activate(tmp_path):
    with pytest.raises(RuntimeError,match='not yet complete'):
        delivery.release_evidence(tmp_path)


def test_complete_business_without_final_report_audits_cannot_activate(tmp_path):
    directory,load=fixture_evidence(tmp_path)
    p=directory/'structured_multi/audit_registration.json'
    original=delivery.read(p); original['status']='registered'; save(p,original)
    with pytest.raises(RuntimeError,match='Both complete'):
        load()


def test_changed_audited_report_inputs_prevent_activation(tmp_path):
    directory,load=fixture_evidence(tmp_path)
    assert load()['selected_arm']=='structured_multi'
    save(directory/'structured_multi/audit/inputs.json',[{'unexpected':'changed report'}])
    with pytest.raises(ValueError,match='actual run evidence'):
        load()


def test_changed_business_totals_are_not_presented_as_measured(tmp_path):
    directory,load=fixture_evidence(tmp_path)
    p=directory/'summary.json'; summary=delivery.read(p); summary['arms']['structured_multi']['passed']=159; save(p,summary)
    with pytest.raises(ValueError,match='individual results'):
        load()


def test_selected_runner_uses_real_source_renderer_and_preserves_state(tmp_path,monkeypatch):
    store=Store(Catalog(),tmp_path/'store.sqlite'); sid=store.session()['id']
    monkeypatch.setattr(delivery,'StructuredReportAgent',lambda **kw:StructuredReportAgent(
        **kw,client=Client([done(ids=[],status='needs_clarification',missing=['shipping_budget'])])))
    before=store.cart(sid)
    result=delivery.run_selected(sid,'请补充运输预算后再执行。',store=store)
    assert result['report_contract_version']==delivery.REPORT_VERSION
    assert result['report']['status']=='needs_clarification'
    assert '运费预算' in result['report']['answer']
    assert store.cart(sid)==before and store.orders(sid)==[]
    assert store.run(sid,result['run_id'])['status']=='completed'


def test_wrong_model_fails_without_calling_a_substitute(tmp_path,monkeypatch):
    store=Store(Catalog(),tmp_path/'store.sqlite'); sid=store.session()['id']
    def forbidden(**kwargs):
        raise AssertionError('No model should be constructed')
    monkeypatch.setattr(delivery,'StructuredReportAgent',forbidden)
    result=delivery.run_selected(sid,'Describe a shirt',store=store,model='qwen3.8-max')
    assert result['error_type']=='ConfigurationMismatch' and result['model_calls']==0
    assert store.run(sid,result['run_id'])['status']=='failed'


def test_page_and_status_report_only_the_measured_execution_config(tmp_path):
    store=Store(Catalog(),tmp_path/'store.sqlite')
    app=delivery.create_delivery_app(store,testing=True,evidence_loader=lambda:{'status':'fixture-selection'})
    with TestClient(app) as client:
        page=client.get('/').text
        assert 'value="gpt-5.6-luna"' in page and 'value="qwen3.8-max"' not in page
        status=client.get('/api/status').json()
        assert status['models']==[delivery.MODEL] and status['evaluation_status']=='fixture-selection'
        assert client.get('/api/cart',headers={'X-Session-Id':'not-a-session'}).status_code==401


def test_status_polling_keeps_large_integrity_manifest_out_of_repeated_responses(tmp_path):
    store=Store(Catalog(),tmp_path/'store.sqlite')
    certification={'status':'fixture-selection','sources_sha256':{f'evidence/file-{i}.json':'a'*64 for i in range(5000)}}
    app=delivery.create_delivery_app(store,testing=True,evidence_loader=lambda:certification)
    with TestClient(app) as client:
        response=client.get('/api/status')
        assert len(response.content)<10000
        value=response.json()['evaluation']
        assert value['source_file_count']==5000 and 'sources_sha256' not in value
        assert len(value['source_manifest_sha256'])==64
        assert client.get('/api/evaluation/evidence').json()==certification

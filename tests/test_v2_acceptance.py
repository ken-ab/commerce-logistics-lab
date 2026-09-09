"""Offline checks for selection evidence and non-resampling final execution."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from evaluation.business_metrics import summarize
from evaluation_v2 import audit, freeze, gate, run_final
from evaluation_v2.communication import signature


def put(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


@pytest.fixture
def validation(tmp_path, monkeypatch):
    monkeypatch.setattr(gate,'ROOT',tmp_path)
    method = tmp_path/'evaluation/report_judge.py'
    method.parent.mkdir()
    method.write_text('fixture evaluator',encoding='utf-8')
    for name in ('research/model_client.py','research/rate_card.json'):
        path=tmp_path/name; path.parent.mkdir(exist_ok=True); path.write_text('fixture',encoding='utf-8')
    monkeypatch.setattr(gate,'method_files',lambda:[method])
    cases=[{'id':f'fixture-{i:02}','family':'describe_only','task':'Describe the item without changes.',
        'partition':'validation','response_language':'en'} for i in range(32)]
    manifest={'sha256':'fixture-manifest'}
    for module in (gate,audit):
        monkeypatch.setattr(module,'load_cases',lambda:(cases,manifest))
    caldir=tmp_path/'communication-calibration'
    put(caldir/'summary.json',{'scheduled':16,'calibration_agreements':16,'communication':signature()})
    put(caldir/'results.json',[{'id':f'cal-{i}','agrees_with_fixture':True} for i in range(16)])
    put(tmp_path/'evidence/v2_communication_calibration_registration.json',
        {'status':'complete','directory':str(caldir),'summary_sha256':gate.sha(caldir/'summary.json')})
    factdir=tmp_path/'evidence/report_audits/20260907T142518252421Z_calibration'
    put(factdir/'config.json',{'model':'qwen3.8-max','judge_version':gate.FACT_VERSION,
        'system_prompt_sha256':hashlib.sha256(gate.FACT_SYSTEM.encode()).hexdigest(),
        'code_sha256':gate.sha(method),'client_code_sha256':gate.sha(tmp_path/'research/model_client.py'),
        'rate_card_sha256':gate.sha(tmp_path/'research/rate_card.json')})
    put(factdir/'results.json',[{'id':c['id'],'agrees_with_fixture':True} for c in gate.fact_fixtures()])
    put(factdir/'summary.json',{'scope':'offline gate fixture, not an actual judge result'})
    directory=tmp_path/'validation'
    put(tmp_path/'evidence/v2_validation_registration.json',{'status':'complete','directory':str(directory)})
    for arm in gate.ARMS:
        folder=directory/arm
        put(folder/'config.json',{'arm':arm,'arm_order':list(gate.ARMS),'model':gate.MODEL,'topology':'multi',
            'workers':2,'code_sha256':{'evaluation/report_judge.py':gate.sha(method)},'partition':'validation',
            'case_manifest_sha256':manifest['sha256'],'case_ids':[c['id'] for c in cases]})
        rows=[]
        for case in cases:
            rows.append({'case_id':case['id'],'family':case['family'],'run_status':'completed',
                'score':{'passed':True},'settled_cost_cny':'0.1','latency_seconds':10})
            snapshot={'cart':{'items':[]},'latest_proposal':None,'orders':[]}
            put(folder/case['id']/'initial_state.json',snapshot)
            put(folder/case['id']/'live_state.json',snapshot)
            put(folder/case['id']/'actual_run.json',{'status':'completed','traces':[],
                'result':{'report':{'answer':'Fixture description, USD 10.00. Cart unchanged.',
                    'product_ids':[],'proposal_id':None,'status':'completed'}}})
        put(folder/'results.json',rows)
        put(folder/'summary.json',summarize(rows))
        adir=tmp_path/('audit-'+arm)
        inputs,_=audit.campaign_items(folder)
        put(adir/'inputs.json',inputs)
        put(adir/'config.json',{'mode':'campaign','judge_model':'qwen3.8-max','facts_version':gate.FACT_VERSION,
            'campaign':str(folder),'case_ids':[c['id'] for c in cases],'workers':2})
        (adir/'report_judge.py').write_bytes(method.read_bytes())
        put(adir/'results.json',[{'id':c['id'],'facts':{'decision':{'verdict':'supported'}},
            'communication':{'passed':True}} for c in cases])
        refresh_audit(folder,adir)
    return directory


def refresh_audit(folder,adir):
    rows=gate.read(adir/'results.json')
    put(adir/'summary.json',{**gate.read(adir/'config.json'),'scheduled':32,'source_results_sha256':gate.sha(folder/'results.json'),
        'communication':signature(),'facts_supported':sum(r['facts']['decision']['verdict']=='supported' for r in rows),
        'communication_passed':sum(r['communication']['passed'] for r in rows)})
    put(folder/'audit_registration.json',{'status':'complete','directory':str(adir),'summary_sha256':gate.sha(adir/'summary.json')})


def test_complete_predeclared_validation_is_eligible(validation):
    result=gate.assess(validation)
    assert result['passed'] and result['arms']['structured_multi']['counts']['joint']==32


def test_three_unsupported_reports_cannot_pass(validation):
    folder=validation/'structured_multi'; adir=validation.parent/'audit-structured_multi'
    rows=gate.read(adir/'results.json')
    for row in rows[:3]: row['facts']['decision']['verdict']='insufficient_evidence'
    put(adir/'results.json',rows); refresh_audit(folder,adir)
    result=gate.assess(validation)
    assert not result['passed'] and not result['checks']['facts_minimum']


def test_one_unauthorized_cart_change_is_critical(validation):
    folder=validation/'structured_multi'; adir=validation.parent/'audit-structured_multi'
    path=folder/'fixture-00/live_state.json'; state=gate.read(path)
    state['cart']['items']=[{'product_id':'unexpected','quantity':1}]; put(path,state)
    # Even an overly favorable model auditor must not override the state guard.
    inputs,_=audit.campaign_items(folder); put(adir/'inputs.json',inputs)
    result=gate.assess(validation)
    assert not result['passed'] and not result['checks']['no_critical_issues']


def test_report_changed_after_audit_is_rejected(validation):
    path=validation/'structured_multi/fixture-00/actual_run.json'; record=gate.read(path)
    record['result']['report']['answer']='A different unaudited answer.'; put(path,record)
    with pytest.raises(ValueError,match='inputs no longer match'):
        gate.assess(validation)


def test_duplicate_audit_case_is_rejected(validation):
    folder=validation/'structured_multi'; adir=validation.parent/'audit-structured_multi'
    rows=gate.read(adir/'results.json'); rows[-1]=copy.deepcopy(rows[0])
    put(adir/'results.json',rows); refresh_audit(folder,adir)
    with pytest.raises(ValueError,match='duplicate'):
        gate.assess(validation)


def test_changed_method_is_rejected(validation):
    (validation.parent/'evaluation/report_judge.py').write_text('changed method',encoding='utf-8')
    with pytest.raises(ValueError,match='runtime method'):
        gate.assess(validation)


def test_wrong_fact_judge_identity_is_rejected(validation):
    adir=validation.parent/'audit-structured_multi'; config=gate.read(adir/'config.json')
    config['judge_model']='another-judge'; put(adir/'config.json',config)
    refresh_audit(validation/'structured_multi',adir)
    with pytest.raises(ValueError,match='evaluator identity'):
        gate.assess(validation)


def test_failed_gate_cannot_unseal_final(tmp_path,monkeypatch):
    monkeypatch.setattr(freeze,'ROOT',tmp_path)
    monkeypatch.setattr(freeze,'contained',lambda path: path.resolve())
    path=tmp_path/'gate.json'; put(path,{'passed':False})
    with pytest.raises(ValueError,match='passed validation'):
        freeze.validated_selection(path)


def test_final_attempt_is_never_regenerated(tmp_path,monkeypatch):
    (tmp_path/'started').mkdir(); calls=[]
    case={'id':'one','family':'describe_only'}
    def fail(*args):
        calls.append(args); raise RuntimeError('An attempt started and failed')
    monkeypatch.setattr(run_final,'run_case',fail)
    row=run_final.execute(case,tmp_path,'identity_multi')
    assert row['run_status']=='harness_failed' and not row['score']['passed']
    assert run_final.restore_row(tmp_path,case)==row
    with pytest.raises(FileExistsError):
        run_final.execute(case,tmp_path,'identity_multi')
    assert len(calls)==1


def test_interruption_is_retained_as_failure(tmp_path):
    (tmp_path/'started').mkdir(); case={'id':'interrupted','family':'quote_only'}
    put(tmp_path/'started/interrupted.json',{'case_id':'interrupted'})
    row=run_final.restore_row(tmp_path,case)
    assert row['run_status']=='interrupted' and not row['score']['passed']


def test_all_final_cases_scheduled_once_deterministically():
    cases=[{'id':f'case-{i}'} for i in range(160)]
    arms=['identity_multi','structured_multi']
    jobs=run_final.schedule(cases,arms,'fixed')
    assert len(jobs)==len({(arm,c['id']) for arm,c in jobs})==320
    assert jobs==run_final.schedule(list(reversed(cases)),list(reversed(arms)),'fixed')


def test_frozen_files_and_selection_are_checked(tmp_path,monkeypatch):
    monkeypatch.setattr(freeze,'ROOT',tmp_path)
    method=tmp_path/'method.py'; method.write_text('fixed',encoding='utf-8')
    monkeypatch.setattr(freeze,'method_files',lambda:[method])
    for name in freeze.DATA_FILES: put(tmp_path/name,{})
    put(tmp_path/'gate.json',{'passed':True})
    paths=[method,*[tmp_path/n for n in freeze.DATA_FILES],tmp_path/'gate.json']
    frozen={'status':freeze.STATUS,'expected_cases_per_arm':160,'arms':list(gate.ARMS),
        'model':gate.MODEL,'workers':2,'case_ids':[str(i) for i in range(160)],
        'gate':'gate.json','gate_sha256':freeze.sha(tmp_path/'gate.json'),
        'files':{p.relative_to(tmp_path).as_posix():freeze.sha(p) for p in paths}}
    output=tmp_path/'freeze.json'; put(output,frozen)
    assert freeze.validate(output,root=tmp_path,check_runtime=False)==frozen
    method.write_text('edited',encoding='utf-8')
    with pytest.raises(ValueError,match='Frozen v2 input changed'):
        freeze.validate(output,root=tmp_path,check_runtime=False)


def test_frozen_evidence_cannot_leave_project(tmp_path):
    with pytest.raises(ValueError,match='leaves the project'):
        freeze.contained(tmp_path/'../outside.json',tmp_path)

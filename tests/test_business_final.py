import json
from pathlib import Path
import pytest

from evaluation.business_metrics import paired, summarize


def test_paired_summary_requires_all_cases_and_preserves_cluster_dependence():
    cases=[{'id':f'{g}-{i}','product_id':g} for g in ['A','B'] for i in range(4)]
    base=[{'case_id':c['id'],'score':{'passed':False}} for c in cases]
    other=[{'case_id':c['id'],'score':{'passed':c['product_id']=='A'}} for c in cases]
    comparison=paired(base,other,cases)
    assert comparison['pass_rate_difference']==.5
    assert comparison['cluster_percentile_95_interval']==[0,1]
    with pytest.raises(ValueError,match='every case'):
        paired(base,other[:-1],cases)


def test_harness_failure_remains_in_summary_denominator():
    rows=[{'case_id':'A','family':'stock','run_status':'completed','score':{'passed':True},
        'settled_cost_cny':'0.1','latency_seconds':2},
        {'case_id':'B','family':'stock','run_status':'harness_failed','score':{'passed':False}}]
    result=summarize(rows)
    assert result['cases']==2 and result['passed']==1 and result['settled_cost_cny']=='0.1'


def test_final_resume_never_resamples_started_attempt(tmp_path):
    pytest.importorskip('tau2')
    from evaluation.run_final import restore_row
    case={'id':'case-a','family':'stock'}
    assert restore_row(tmp_path,case) is None
    (tmp_path/'started').mkdir()
    (tmp_path/'started/case-a.json').write_text('{}')
    assert restore_row(tmp_path,case)['score']['passed'] is False
    (tmp_path/'case-a').mkdir()
    saved={'case_id':'case-a','family':'stock','score':{'passed':True}}
    (tmp_path/'case-a/trial_result.json').write_text(json.dumps(saved))
    assert restore_row(tmp_path,case)==saved


def test_second_final_worker_cannot_acquire_same_run_lock(tmp_path):
    pytest.importorskip('tau2')
    from evaluation.run_final import exclusive_run
    with exclusive_run(tmp_path/'run.lock'):
        with pytest.raises(OSError):
            with exclusive_run(tmp_path/'run.lock'):
                pytest.fail('Duplicate worker acquired the lock')
    with exclusive_run(tmp_path/'run.lock'):
        pass


def test_method_change_invalidates_business_resume(tmp_path,monkeypatch):
    import evaluation.business_freeze as freeze
    monkeypatch.setattr(freeze,'METHOD_FILES',('method.py',))
    source=tmp_path/'method.py'; source.write_text('original')
    manifest=tmp_path/'freeze.json'
    manifest.write_text(json.dumps({'status':'frozen_before_final_business_test','expected_cases_per_arm':160,
        'files':{'method.py':freeze.sha(source)}}))
    freeze.validate(manifest,root=tmp_path,check_runtime=False)
    source.write_text('changed')
    with pytest.raises(ValueError,match='changed'):
        freeze.validate(manifest,root=tmp_path,check_runtime=False)


def test_rejected_diagnostic_cannot_activate_candidate_or_bypass_evidence_failure():
    from evaluation.business_freeze import check_selection
    base={'id':'baseline'}; candidate={'id':'candidate'}
    gate={'eligible':False,'candidate_policy':candidate,'baseline_id':'baseline',
        'reasons':['Unresolved or unsupported report claims: case-a']}
    with pytest.raises(ValueError,match='explicit'):
        check_selection(gate,base)
    selection=check_selection(gate,base,include_rejected=True)
    assert selection['eligible_for_activation'] is False
    assert selection['known_rejection_reasons']==gate['reasons']
    with pytest.raises(ValueError,match='inactive'):
        check_selection(gate,candidate,include_rejected=True)
    gate['reasons'].append('Paired business implementation differs: code.py')
    with pytest.raises(ValueError,match='complete, matched'):
        check_selection(gate,base,include_rejected=True)


def test_passed_candidate_must_be_active_before_final_freeze():
    from evaluation.business_freeze import check_selection
    candidate={'id':'candidate'}
    gate={'eligible':True,'candidate_policy':candidate}
    with pytest.raises(ValueError,match='validated active'):
        check_selection(gate,{'id':'baseline'})
    assert check_selection(gate,candidate)['eligible_for_activation'] is True

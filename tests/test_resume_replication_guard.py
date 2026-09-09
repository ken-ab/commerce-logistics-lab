"""Offline operational guard checks; never import or call a live model client."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest
import resume_replication as guard


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


def fixture(root,status='registered'):
    directory=root/'evidence/audit_replication_runs/fixture_test'
    (directory/'transport').mkdir(parents=True)
    save(root/'evidence/audit_replication_test_registration.json',
         {'status':status,'partition':'test','directory':str(directory)})
    save(directory/'progress.json',{'status':'running','completed':52,'total':320})
    return directory


def test_old_denial_does_not_cancel_restored_account_before_a_new_response(tmp_path):
    save(tmp_path/'old.json',{'http_status':400,'hostname':guard.HOST})
    observer=guard.RejectionObserver(tmp_path)
    assert observer.scan() is None
    save(tmp_path/'new.json',{'http_status':403,'hostname':guard.HOST,'phase':'response_body'})
    event=observer.scan()
    assert event['http_status']==403 and event['trace_file'].endswith('new.json')


def test_incomplete_record_is_rechecked_and_nonmatching_host_is_ignored(tmp_path):
    observer=guard.RejectionObserver(tmp_path)
    pending=tmp_path/'pending.json'; pending.write_text('{')
    save(tmp_path/'other.json',{'http_status':400,'hostname':'unrelated.example'})
    assert observer.scan() is None
    save(pending,{'hostname':guard.HOST,'status':'started'})
    assert observer.scan() is None
    save(pending,{'hostname':guard.HOST,'http_status':402})
    assert observer.scan()['http_status']==402


@pytest.mark.parametrize('status',[200,429,500,502,503,504])
def test_original_success_and_transient_retry_policy_are_not_reselected(tmp_path,status):
    observer=guard.RejectionObserver(tmp_path)
    save(tmp_path/'fresh.json',{'http_status':status,'hostname':guard.HOST})
    assert observer.scan() is None


def test_rejection_saves_operational_record_and_exits_own_process_without_rewriting_results(tmp_path):
    directory=fixture(tmp_path)
    source=directory/'structured_multi/trial_result.json'; save(source,{'unchanged':True})
    before=source.read_bytes()
    observer=guard.RejectionObserver(directory/'transport')
    save(directory/'transport/new.json',{'http_status':400,'hostname':guard.HOST,'phase':'response_body'})
    exit_process=Mock()
    guard.supervise(observer,threading.Event(),tmp_path,directory,exit_process=exit_process,poll_seconds=.001)
    exit_process.assert_called_once_with(75)
    records=list((tmp_path/'evidence/replication_service_pauses').glob('*.json'))
    record=guard.read(records[0])
    assert record['event']['http_status']==400 and record['last_progress']['completed']==52
    assert source.read_bytes()==before


def test_no_restoration_acknowledgement_refuses_before_validation_or_launch(tmp_path):
    validator,runner=Mock(),Mock()
    with pytest.raises(SystemExit):
        guard.main([],root=tmp_path,validator=validator,runner=runner)
    validator.assert_not_called(); runner.assert_not_called()


def test_dry_run_and_completed_registration_never_launch_the_evaluator(tmp_path):
    fixture(tmp_path); validator,runner=Mock(),Mock()
    guard.main(['--dry-run'],root=tmp_path,validator=validator,runner=runner)
    validator.assert_called_once(); runner.assert_not_called()
    registration=tmp_path/'evidence/audit_replication_test_registration.json'
    value=guard.read(registration); value['status']='complete'; save(registration,value)
    guard.main(['--acknowledge-provider-restored'],root=tmp_path,validator=validator,runner=runner)
    runner.assert_not_called()


def test_launch_is_exact_original_resume_module_and_restores_arguments(tmp_path):
    fixture(tmp_path); original=sys.argv
    observed=[]
    def runner(module,**kwargs):
        observed.append((module,kwargs,list(sys.argv)))
    guard.main(['--acknowledge-provider-restored'],root=tmp_path,validator=Mock(),runner=runner)
    assert observed==[('audit_replication.run',{'run_name':'__main__'},
                      ['audit_replication.run','--partition','test','--resume'])]
    assert sys.argv is original


def test_guard_observation_failure_stops_instead_of_leaving_paid_work_unwatched(tmp_path):
    directory=fixture(tmp_path)
    observer=Mock(); observer.scan.side_effect=OSError('fixture observation unavailable')
    exit_process=Mock()
    guard.supervise(observer,threading.Event(),tmp_path,directory,exit_process=exit_process,poll_seconds=.001)
    exit_process.assert_called_once_with(75)
    record=guard.read(next((tmp_path/'evidence/replication_service_pauses').glob('*.json')))
    assert record['event']=={'reason':'Guard observation failed','error_type':'OSError'}


def test_real_exit_ends_only_the_fixture_process_before_following_work(tmp_path):
    directory=fixture(tmp_path)
    code="""
import json,sys,threading
from pathlib import Path
import resume_replication as g
r=Path(sys.argv[1]); d=r/'evidence/audit_replication_runs/fixture_test'
observer=g.RejectionObserver(d/'transport')
(d/'transport/new.json').write_text(json.dumps({'http_status':400,'hostname':g.HOST}))
g.supervise(observer,threading.Event(),r,d,poll_seconds=.001)
(r/'unexpected_following_work').write_text('must not run')
"""
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],cwd=guard.ROOT,
        capture_output=True,text=True,timeout=10,creationflags=flags)
    assert result.returncode==75
    assert not (tmp_path/'unexpected_following_work').exists()
    assert len(list((tmp_path/'evidence/replication_service_pauses').glob('*.json')))==1

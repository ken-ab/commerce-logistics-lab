"""Run the unchanged replication CLI with an external account-rejection stop guard.

No prompts, scores, case scheduling, retry limits or budget accounting are changed.
A new permanent HTTP rejection stops this process; started incomplete work follows
the original CLI's no-resampling recovery rule. No service probe is made here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import threading

ROOT=Path(__file__).resolve().parent
HOST='dashscope.aliyuncs.com'
PERMANENT_REJECTIONS={400,401,402,403,404,405,410,422}
EXIT_PROVIDER_REJECTED=75


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


class RejectionObserver:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.seen={p.name for p in self.directory.glob('*.json')}

    def scan(self):
        for path in sorted(self.directory.glob('*.json')):
            if path.name in self.seen:
                continue
            try:
                value=read(path)
            except (FileNotFoundError,json.JSONDecodeError):
                continue  # A still-being-written record is checked again next time.
            status=value.get('http_status')
            if type(status) is not int:
                continue
            self.seen.add(path.name)
            if value.get('hostname')==HOST and status in PERMANENT_REJECTIONS:
                return {'trace_file':str(path),'trace_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'hostname':HOST,'http_status':status,'phase':value.get('phase')}
        return None


def record_stop(root,directory,event):
    stamp=datetime.now(timezone.utc)
    folder=root/'evidence/replication_service_pauses'
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/(stamp.strftime('%Y%m%dT%H%M%S%fZ')+f'_{os.getpid()}.json')
    try:
        progress=read(directory/'progress.json')
    except (OSError,json.JSONDecodeError):
        progress=None
    record={'status':'stopped_after_new_permanent_reviewer_rejection','created_at':stamp.isoformat(),
        'pid':os.getpid(),'directory':str(directory),'event':event,'last_progress':progress,
        'scope':'Operational pause only; no score, policy, retry limit or case membership changed. '
                'In-flight work may be incomplete; original no-resampling restore rule applies. '
                'All outstanding reservations remain in the shared ledger.'}
    with target.open('x',encoding='utf-8') as handle:
        json.dump(record,handle,ensure_ascii=False,indent=2)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    return target


def supervise(observer,stop_event,root,directory,*,exit_process=os._exit,poll_seconds=.5):
    while not stop_event.wait(poll_seconds):
        try:
            event=observer.scan()
        except Exception as error:
            event={'reason':'Guard observation failed','error_type':type(error).__name__}
        if event:
            try:
                target=record_stop(root,directory,event)
                print({'stopped':'new reviewer rejection or observation failure','pause_record':str(target)},flush=True)
            finally:
                # This is the evaluator's own process, not a guessed or unrelated PID.
                exit_process(EXIT_PROVIDER_REJECTED)
            return


def main(argv=None,*,root=ROOT,validator=None,runner=None,exit_process=os._exit):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acknowledge-provider-restored',action='store_true',
        help='Caller confirms the previously rejected reviewer service has been restored. This flag is not a health test.')
    parser.add_argument('--dry-run',action='store_true',help='Validate local resume prerequisites; no model or service request.')
    args=parser.parse_args(argv)
    if not args.dry_run and not args.acknowledge_provider_restored:
        parser.error('Restore the original reviewer account before acknowledging and resuming')
    root=Path(root).resolve()
    registration=read(root/'evidence/audit_replication_test_registration.json')
    directory=Path(registration['directory']).resolve()
    if (registration.get('partition')!='test' or not directory.is_relative_to(root/'evidence/audit_replication_runs')
            or not directory.is_dir()):
        raise ValueError('Resume must use the existing test registration inside this project')
    if validator is None:
        from audit_replication.method import validate_final
        validator=validate_final
    validator()
    if registration.get('status')=='complete':
        print({'already_complete':str(directory)})
        return
    if args.dry_run:
        print({'local_prerequisites':'valid','directory':str(directory),'model_calls':0,
               'provider_health':'not checked; restoration is still required',
               'command':['audit_replication.run','--partition','test','--resume']})
        return
    observer=RejectionObserver(directory/'transport')
    stop_event=threading.Event()
    watcher=threading.Thread(target=supervise,args=(observer,stop_event,root,directory),
        kwargs={'exit_process':exit_process},name='reviewer-rejection-stop-guard',daemon=True)
    old_argv=sys.argv
    sys.argv=['audit_replication.run','--partition','test','--resume']
    watcher.start()
    try:
        (runner or runpy.run_module)('audit_replication.run',run_name='__main__')
    finally:
        stop_event.set(); watcher.join(timeout=2); sys.argv=old_argv


if __name__=='__main__':
    main()

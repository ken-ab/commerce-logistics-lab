"""One registered final experiment; interrupted attempts cannot be resampled."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from evaluation.business_freeze import read, sha, validate
from evaluation.business_metrics import paired, summarize
from evaluation.run_campaign import run_case
from research.model_client import BudgetedChatClient
from research.model_config import ROOT


def save(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,path)


@contextmanager
def exclusive_run(path):
    """A kernel-owned lock releases on process exit; no stale PID guessing."""
    with path.open('a+b') as lock:
        if lock.tell()==0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        acquired=False
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
            yield
        finally:
            if acquired:
                lock.seek(0)
                if os.name=='nt':
                    msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def schedule(cases, arms, salt):
    digest=lambda text:hashlib.sha256((salt+':'+text).encode()).hexdigest()
    return [(arm,case) for case in sorted(cases,key=lambda c:digest(c['id']))
        for arm in sorted(arms,key=lambda a:digest(case['id']+':'+a))]


def interrupted(case):
    return {'case_id':case['id'],'family':case['family'],'run_status':'interrupted',
        'score':{'passed':False},'error':'An earlier attempt started without a complete result. It is retained as a failure and is not regenerated.'}


def restore_row(directory, case):
    finished=directory/case['id']/'trial_result.json'
    if finished.exists():
        row=read(finished)
        if row['case_id']!=case['id'] or row['family']!=case['family']:
            raise ValueError('Saved final result identity mismatch')
        return row
    if (directory/'started'/f"{case['id']}.json").exists() or (directory/case['id']).exists():
        return interrupted(case)
    return None


def execute(case, directory, frozen, arm):
    started=directory/'started'/f"{case['id']}.json"
    with started.open('x',encoding='utf-8') as out:
        json.dump({'started_at':datetime.now(timezone.utc).isoformat(),'case_id':case['id']},out)
    try:
        row=run_case(case,directory,frozen['model'],arm['policy'],arm['topology'])
    except Exception as error:
        row={'case_id':case['id'],'family':case['family'],'run_status':'harness_failed',
            'score':{'passed':False},'error':type(error).__name__+': '+str(error)[:800]}
    case_dir=directory/case['id']; case_dir.mkdir(exist_ok=True)
    save(case_dir/'trial_result.json',row)
    return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--freeze',type=Path,default=ROOT/'evidence/business_final_freeze.json')
    parser.add_argument('--resume',type=Path)
    args=parser.parse_args()
    frozen=validate(args.freeze)
    cases=[c for c in read(ROOT/'data/commerce_cases_v1.json')['cases'] if c['partition']=='test']
    if len(cases)!=frozen['expected_cases_per_arm']:
        raise ValueError('Final case membership changed')
    registry_path=ROOT/'evidence/business_final_registration.json'
    if args.resume:
        directory=args.resume.resolve()
        registration=read(registry_path)
        if directory!=Path(registration['directory']) or sha(args.freeze)!=registration['freeze_sha256']:
            raise ValueError('Resume does not match the sole registered experiment')
        directory.mkdir(parents=True,exist_ok=True)
    else:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory=ROOT/'evidence/final_business'/stamp
        registration={'directory':str(directory),'freeze_sha256':sha(args.freeze),'created_at':stamp}
        with registry_path.open('x',encoding='utf-8') as out:
            json.dump(registration,out,ensure_ascii=False,indent=2)
        directory.mkdir(parents=True)
        save(directory/'frozen_method.json',frozen)
    if (directory/'frozen_method.json').exists() and read(directory/'frozen_method.json')!=frozen:
        raise ValueError('Saved final method differs from the registered freeze')
    if not (directory/'frozen_method.json').exists():
        save(directory/'frozen_method.json',frozen)
    with exclusive_run(directory/'run.lock'):
        rows={name:[] for name in frozen['arms']}
        pending=[]
        for name,arm in frozen['arms'].items():
            arm_dir=directory/name; arm_dir.mkdir(exist_ok=True)
            (arm_dir/'started').mkdir(exist_ok=True)
            config={'partition':'test','model':frozen['model'],**arm,
                'case_ids':[c['id'] for c in cases],'workers':2,'freeze_sha256':sha(args.freeze),
                'scope':'Final custom synthetic business tasks; actual model calls, not official leaderboard tasks.'}
            config_path=arm_dir/'config.json'
            if config_path.exists() and read(config_path)!=config:
                raise ValueError('Final arm configuration changed')
            if not config_path.exists():
                save(config_path,config)
        for name,case in schedule(cases,frozen['arms'],frozen['order_salt']):
            restored=restore_row(directory/name,case)
            if restored is None:
                pending.append((name,case))
            else:
                rows[name].append(restored)
        save(directory/'progress.json',{'status':'running','completed':sum(map(len,rows.values())),
            'total':len(cases)*len(rows),'remaining':len(pending)})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures={pool.submit(execute,case,directory/name,frozen,frozen['arms'][name]):(name,case) for name,case in pending}
            for future in as_completed(futures):
                name,case=futures[future]
                try:
                    row=future.result()
                except Exception as error:
                    row={'case_id':case['id'],'family':case['family'],'run_status':'harness_failed',
                        'score':{'passed':False},'error':type(error).__name__+': '+str(error)[:800]}
                rows[name].append(row)
                save(directory/name/'results.json',sorted(rows[name],key=lambda r:r['case_id']))
                completed=sum(map(len,rows.values()))
                save(directory/'progress.json',{'status':'running','completed':completed,'total':len(cases)*len(rows)})
                print(json.dumps({'completed':completed,'total':len(cases)*len(rows),'arm':name,
                    'case':case['id'],'passed':row['score']['passed']},ensure_ascii=False),flush=True)
        for name in rows:
            if len(rows[name])!=len(cases) or {r['case_id'] for r in rows[name]}!={c['id'] for c in cases}:
                raise ValueError('Final arm coverage is incomplete')
            save(directory/name/'results.json',sorted(rows[name],key=lambda r:r['case_id']))
            save(directory/name/'summary.json',{'directory':str(directory/name),**summarize(rows[name])})
        summary={'status':'complete','arms':{name:summarize(records) for name,records in rows.items()},
            'paired':{name:paired(rows['baseline_multi'],rows[name],cases) for name in ('candidate_multi','baseline_single')},
            'candidate_selection':frozen['candidate_selection'],
            'global_budget':BudgetedChatClient().ledger.summary(),'report_audit':'pending; business success is not a full report-quality pass'}
        save(directory/'summary.json',summary)
        save(directory/'progress.json',{'status':'complete','completed':len(cases)*len(rows),'total':len(cases)*len(rows)})
        print(json.dumps({'directory':str(directory),'status':'complete','arms':summary['arms']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

"""One resumable v2 final run with no replacement of started attempts."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path

from evaluation.business_metrics import paired, summarize
from evaluation.run_final import exclusive_run, restore_row, save, schedule
from evaluation_v2.freeze import read, sha, validate
from evaluation_v2.prepare import load_cases
from evaluation_v2.run import run_case
from research.model_config import ROOT
from research.model_client import BudgetedChatClient


def execute(case, directory, arm):
    started = directory/'started'/f"{case['id']}.json"
    with started.open('x',encoding='utf-8') as stream:
        json.dump({'case_id':case['id'],'arm':arm,'started_at':datetime.now(timezone.utc).isoformat()},stream)
    try:
        row = run_case(case,directory,arm)
    except Exception as error:
        row = {'case_id':case['id'],'family':case['family'],'arm':arm,'run_status':'harness_failed',
            'score':{'passed':False},'error':type(error).__name__+': '+str(error)[:1000]}
    case_dir = directory/case['id']
    case_dir.mkdir(exist_ok=True)
    save(case_dir/'trial_result.json',row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--freeze',type=Path,default=ROOT/'evidence/v2_final_freeze.json')
    parser.add_argument('--resume',type=Path)
    args = parser.parse_args()
    frozen = validate(args.freeze)
    all_cases, manifest = load_cases()
    cases = [c for c in all_cases if c['partition'] == 'test']
    if [c['id'] for c in cases] != frozen['case_ids'] or manifest['sha256'] != frozen['case_manifest_sha256']:
        raise ValueError('Final case registration changed')
    register = ROOT/'evidence/v2_test_registration.json'
    if args.resume:
        directory = args.resume.resolve()
        registration = read(register)
        if directory != Path(registration['directory']) or registration['freeze_sha256'] != sha(args.freeze):
            raise ValueError('Resume differs from the sole registered final experiment')
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory = ROOT/'evidence/v2_campaigns'/(stamp+'_test')
        registration = {'status':'registered','directory':str(directory),'created_at':stamp,'freeze_sha256':sha(args.freeze)}
        with register.open('x',encoding='utf-8') as stream:
            json.dump(registration,stream,ensure_ascii=False,indent=2)
        directory.mkdir(parents=True)
    with exclusive_run(directory/'run.lock'):
        if (directory/'frozen_method.json').exists() and read(directory/'frozen_method.json') != frozen:
            raise ValueError('Saved frozen method changed')
        save(directory/'frozen_method.json',frozen)
        rows = {arm:[] for arm in frozen['arms']}
        for arm in rows:
            folder = directory/arm
            folder.mkdir(exist_ok=True)
            (folder/'started').mkdir(exist_ok=True)
            config = {'partition':'test','arm':arm,'arm_order':frozen['arms'],'model':frozen['model'],
                'policy':read(Path(read(ROOT/frozen['gate'])['directory'])/arm/'config.json')['policy'],
                'topology':'multi','workers':2,'case_ids':frozen['case_ids'],
                'case_manifest_sha256':manifest['sha256'],'freeze_sha256':sha(args.freeze),
                'scope':'Fresh-product final synthetic business tasks, shared templates, not official leaderboard tasks.'}
            if (folder/'config.json').exists() and read(folder/'config.json') != config:
                raise ValueError('Final arm configuration changed')
            save(folder/'config.json',config)
        pending = []
        for arm, case in schedule(cases,rows,frozen['order_salt']):
            existing = restore_row(directory/arm,case)
            if existing is None:
                pending.append((arm,case))
            else:
                rows[arm].append(existing)
        def progress(status):
            save(directory/'progress.json',{'status':status,'completed':sum(map(len,rows.values())),
                'total':len(cases)*len(rows),'updated_at':datetime.now(timezone.utc).isoformat()})
        progress('running')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(execute,case,directory/arm,arm):(arm,case) for arm,case in pending}
            for future in as_completed(futures):
                arm,case = futures[future]
                try:
                    row = future.result()
                except Exception as error:
                    row = {'case_id':case['id'],'family':case['family'],'arm':arm,'run_status':'harness_failed',
                        'score':{'passed':False},'error':type(error).__name__+': '+str(error)[:1000]}
                rows[arm].append(row)
                save(directory/arm/'results.json',sorted(rows[arm],key=lambda r:r['case_id']))
                progress('running')
                print(json.dumps({'completed':sum(map(len,rows.values())),'total':320,
                    'arm':arm,'case':case['id'],'passed':row['score']['passed']},ensure_ascii=False),flush=True)
        for arm, values in rows.items():
            if len(values) != 160 or {r['case_id'] for r in values} != set(frozen['case_ids']):
                raise ValueError('Final arm is incomplete')
            save(directory/arm/'results.json',sorted(values,key=lambda r:r['case_id']))
            save(directory/arm/'summary.json',summarize(values))
        summary = {'status':'complete','partition':'test','directory':str(directory),
            'arms':{arm:summarize(values) for arm,values in rows.items()},
            'paired':paired(rows['identity_multi'],rows['structured_multi'],cases),
            'global_budget':BudgetedChatClient().ledger.summary(),'report_audit':'pending; not a report-quality pass'}
        save(directory/'summary.json',summary)
        progress('complete')
        save(register,{**registration,'status':'complete','completed_at':datetime.now(timezone.utc).isoformat()})
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()

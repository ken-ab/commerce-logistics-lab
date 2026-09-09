"""Generate and audit every scheduled case once, with durable interruption records."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import time

from audit_reliability.limits import IncrementalBudget
from audit_reliability.transport import ObservedAuditClient, VerifiedAuditTransport
from audit_replication.method import METHOD, FINAL_FREEZE, validate, validate_final, write_once, checked_path
from audit_replication.prepare import load_cases
from evaluation.business_freeze import read, sha
from evaluation.business_metrics import summarize, paired
from evaluation.report_judge import evidence_for, judge
from evaluation.run_final import exclusive_run, save, schedule
from evaluation_v2.communication import audit_communication
from evaluation_v2.run import run_case
from research.model_client import BudgetedChatClient
from research.model_config import ROOT


def audit_inputs(case, folder):
    record_path = folder/'actual_run.json'
    record = read(record_path) if record_path.exists() else None
    report = (record.get('result') or {}).get('report') if record else None
    evidence = None
    if report:
        score_path = folder/'external_score/score.json'
        score = read(score_path) if score_path.exists() else {}
        evidence = evidence_for(record,case['task'],before=read(folder/'initial_state.json'),
            after=read(folder/'live_state.json'),replay_verified=bool(score.get('live_replay_state_matches')))
    return {'id':case['id'],'case':{k:case[k] for k in ('family','task','response_language')},
            'report':report,'evidence':evidence}


def failed(error):
    return {'status':'audit_failed','error':str(error)[:1000],'transport_failures':getattr(error,'attempts',[])}


def interrupted(case, folder, arm):
    business = read(folder/'business_result.json') if (folder/'business_result.json').exists() else {
        'case_id':case['id'],'family':case['family'],'response_language':case['response_language'],
        'arm':arm,'run_status':'interrupted','score':{'passed':False},
        'error':'An earlier attempt started without a complete business result; it is not regenerated.'}
    audit = read(folder/'audit_partial.json') if (folder/'audit_partial.json').exists() else {'id':case['id']}
    for kind in ('facts','communication'):
        audit.setdefault(kind,{'status':'audit_failed','error':'Interrupted before a complete audit; not resampled.'})
    return {'case_id':case['id'],'arm':arm,'business':business,'audit':audit,'interrupted':True}


def restore(folder,case,arm):
    target = folder/case['id']
    if (target/'trial_result.json').exists():
        row = read(target/'trial_result.json')
        if row['case_id']!=case['id'] or row['arm']!=arm or row['business']['family']!=case['family']:
            raise ValueError('Saved trial identity differs from the registration')
        return row
    if (folder/'started'/(case['id']+'.json')).exists() or target.exists():
        target.mkdir(exist_ok=True)
        row = interrupted(case,target,arm)
        save(target/'trial_result.json',row)
        return row
    return None


def execute(case, directory, arm, business_client, audit_client):
    write_once(directory/'started'/(case['id']+'.json'),{
        'case_id':case['id'],'arm':arm,'started_at':datetime.now(timezone.utc).isoformat()})
    folder = directory/case['id']
    try:
        business = run_case(case,directory,arm,client=business_client)
    except Exception as error:
        business = {'case_id':case['id'],'family':case['family'],'response_language':case['response_language'],
            'arm':arm,'run_status':'harness_failed','score':{'passed':False},
            'error':type(error).__name__+': '+str(error)[:1000]}
    folder.mkdir(exist_ok=True)
    save(folder/'business_result.json',business)
    item = audit_inputs(case,folder)
    save(folder/'audit_inputs.json',item)
    audit = {'id':case['id']}
    started = time.monotonic()
    if item['report']:
        try:
            audit['facts'] = {'status':'audited',**judge(item['report']['answer'],item['evidence'],
                client=audit_client,model='qwen3.8-max',raw_path=folder/'raw_facts.json')}
        except Exception as error:
            audit['facts'] = failed(error)
        save(folder/'audit_partial.json',audit)
        try:
            audit['communication'] = {'status':'audited',**audit_communication(item['case'],item['report'],
                client=audit_client,raw_path=folder/'raw_communication.json')}
        except Exception as error:
            audit['communication'] = failed(error)
    else:
        audit.update({kind:{'status':'not_assessable','error':'No completed report; retained in denominator.'}
                      for kind in ('facts','communication')})
    audit['latency_seconds'] = round(time.monotonic()-started,3)
    save(folder/'audit_partial.json',audit)
    row = {'case_id':case['id'],'arm':arm,'business':business,'audit':audit}
    save(folder/'trial_result.json',row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--partition',required=True,choices=['validation','test'])
    parser.add_argument('--resume',action='store_true')
    args = parser.parse_args()
    frozen = validate()
    if args.partition=='test':
        validate_final()
    cases,_ = load_cases()
    cases = [c for c in cases if c['partition']==args.partition]
    register = ROOT/f'evidence/audit_replication_{args.partition}_registration.json'
    if args.resume:
        registration = read(register)
        directory = checked_path(registration['directory'])
        if registration['method_sha256']!=sha(METHOD) or registration['partition']!=args.partition:
            raise ValueError('Resume differs from the unique registered replication')
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory = ROOT/'evidence/audit_replication_runs'/(stamp+'_'+args.partition)
        registration = {'status':'registered','directory':str(directory),'partition':args.partition,
            'created_at':stamp,'method_sha256':sha(METHOD)}
        if args.partition=='test':
            registration['final_freeze_sha256'] = sha(FINAL_FREEZE)
        write_once(register,registration)
        directory.mkdir(parents=True)
    with exclusive_run(directory/'run.lock'):
        if registration.get('status')=='complete':
            print({'already_complete':str(directory)})
            return
        save(directory/'config.json',{'partition':args.partition,'method_sha256':sha(METHOD),
            'model':frozen['model'],'arms':frozen['arms'],'workers':2,
            'case_ids':[c['id'] for c in cases],'case_manifest_sha256':frozen['case_manifest_sha256']})
        business_client = BudgetedChatClient()
        budget = IncrementalBudget(business_client.ledger,0)
        # Both phases use the same absolute accounting window registered before validation.
        budget.start = Decimal(frozen['starting_global_budget']['accounted_and_reserved_cny'])
        budget.ceiling = budget.start+Decimal(frozen['maximum_increment_cny'])
        business_client.ledger = budget
        transport = VerifiedAuditTransport(directory/'transport')
        audit_client = ObservedAuditClient(transport=transport)
        audit_client.ledger = budget
        rows = {arm:[] for arm in frozen['arms']}
        for arm in rows:
            folder = directory/arm
            folder.mkdir(exist_ok=True)
            (folder/'started').mkdir(exist_ok=True)
        pending = []
        for arm,case in schedule(cases,rows,frozen['order_salt']+args.partition):
            prior = restore(directory/arm,case,arm)
            if prior is None:
                pending.append((arm,case))
            else:
                rows[arm].append(prior)
        def progress(status):
            save(directory/'progress.json',{'status':status,'completed':sum(map(len,rows.values())),
                'total':len(cases)*len(rows),'updated_at':datetime.now(timezone.utc).isoformat()})
        progress('running')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(execute,case,directory/arm,arm,business_client,audit_client):(arm,case)
                           for arm,case in pending}
                for future in as_completed(futures):
                    arm,case = futures[future]
                    try:
                        row = future.result()
                    except Exception as error:
                        # Preserve completed business/audit components; never rerun a started trial.
                        folder = directory/arm/case['id']
                        folder.mkdir(exist_ok=True)
                        row = interrupted(case,folder,arm)
                        row['orchestration_error'] = type(error).__name__+': '+str(error)[:1000]
                        save(folder/'trial_result.json',row)
                    rows[arm].append(row)
                    save(directory/arm/'results.json',sorted(rows[arm],key=lambda r:r['case_id']))
                    progress('running')
                    print({'completed':sum(map(len,rows.values())),'total':len(cases)*2,
                        'arm':arm,'case':case['id'],'business':row['business']['score']['passed'],
                        'facts':row['audit'].get('facts',{}).get('decision',{}).get('verdict'),
                        'communication':row['audit'].get('communication',{}).get('passed')},flush=True)
        finally:
            transport.close()
        summaries = {}
        for arm,values in rows.items():
            if len(values)!=len(cases) or {r['case_id'] for r in values}!={c['id'] for c in cases}:
                raise ValueError('Incomplete replication arm')
            save(directory/arm/'results.json',sorted(values,key=lambda r:r['case_id']))
            summaries[arm] = summarize([r['business'] for r in values])
        validate()
        comparison = paired([r['business'] for r in rows['identity_multi']],
                            [r['business'] for r in rows['structured_multi']],cases)
        comparison['scope'] = f"{len(cases)//8} fresh product groups with known shared synthetic templates; not real users."
        summary = {'status':'complete','partition':args.partition,'directory':str(directory),'arms':summaries,
            'paired_business':comparison,'scheduled_business':len(cases)*2,'scheduled_report_audits':len(cases)*4,
            'global_budget':budget.summary(),'method_sha256':sha(METHOD)}
        save(directory/'summary.json',summary)
        progress('complete')
        save(register,{**registration,'status':'complete','summary_sha256':sha(directory/'summary.json'),
            'completed_at':datetime.now(timezone.utc).isoformat()})
        print(summary,flush=True)


if __name__=='__main__':
    main()

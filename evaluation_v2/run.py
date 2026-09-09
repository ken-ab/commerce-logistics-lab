"""Paired, case-interleaved v2 runs; no case replacement or test-set tuning."""
import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
from pathlib import Path
import statistics
import time

from commerce_lab.skills import BASELINE
from commerce_lab_v2.agent import IdentityAwareCommerceAgent
from commerce_lab_v2.structured import StructuredReportAgent
from evaluation.environment import business_snapshot, create_case_store
from evaluation.tau_bridge import evaluate_record
from evaluation.report_judge import save
from evaluation_v2.prepare import load_cases
from research.model_config import ROOT
from research.model_client import BudgetedChatClient

ARMS = {'identity_multi': IdentityAwareCommerceAgent, 'structured_multi': StructuredReportAgent}
MODEL = 'gpt-5.6-luna'


def run_case(case, directory, arm, *, client=None, catalog=None):
    folder = directory/case['id']
    folder.mkdir()
    store, sid = create_case_store(case,folder/'live.sqlite',catalog=catalog)
    save(folder/'initial_state.json',business_snapshot(store,sid))
    agent = ARMS[arm](store=store,client=client,model=MODEL,policy=BASELINE,topology='multi')
    started = time.monotonic()
    result = asyncio.run(agent.run(sid,case['task']))
    elapsed = time.monotonic()-started
    record = store.run(sid,result['run_id'])
    record.pop('session_id',None)
    snapshot = business_snapshot(store,sid,agent.last_quote)
    save(folder/'actual_run.json',record)
    save(folder/'live_state.json',snapshot)
    try:
        score = evaluate_record(case,record,snapshot,folder/'external_score',policy=BASELINE,catalog=catalog)
    except Exception as error:
        score = {'passed':False,'evaluation_error':type(error).__name__+': '+str(error)[:1200]}
        save(folder/'grading_error.json',score)
    return {'case_id':case['id'],'family':case['family'],'response_language':case['response_language'],
        'arm':arm,'run_status':record['status'],'score':score,'latency_seconds':round(elapsed,3),
        'model_calls':result['model_calls'],
        'settled_cost_cny':result.get('estimated_cost_cny',result.get('estimated_settled_cost_cny','0')),
        'error':result.get('error')}


def method_files():
    files = [p for folder in ('commerce_lab','commerce_lab_v2','evaluation','evaluation_v2','research','logistics_lab')
             for p in (ROOT/folder).glob('*.py')]
    files += [ROOT/'research/rate_card.json',ROOT/'research/budget_policy.json',ROOT/'research/V2_EXPERIMENT_PROTOCOL.md']
    # Editable dependencies must be bound to their source, not only a version.
    for package in ('tau2','commerce_common','shopping_agent'):
        module = __import__(package)
        files.extend(Path(module.__file__).resolve().parent.rglob('*.py'))
    return sorted(set(files))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--partition',choices=['development','validation','test'],required=True)
    parser.add_argument('--revision',type=int,choices=[1,2],default=1)
    args = parser.parse_args()
    if args.partition == 'test':
        raise ValueError('Final test remains sealed until the v2 validation gate and method freeze are complete')
    if args.revision != 1 and args.partition != 'development':
        raise ValueError('A development bug-fix revision does not permit repeating validation')
    if args.partition == 'validation':
        prior = ROOT/'evidence/v2_development_revision2_registration.json'
        if not prior.exists():
            prior = ROOT/'evidence/v2_development_registration.json'
        development = json.loads(prior.read_text(encoding='utf-8'))
        if development['status'] != 'complete':
            raise ValueError('Development generation must be complete')
        for arm in ARMS:
            audit = Path(development['directory'])/arm/'audit_registration.json'
            if not audit.exists() or json.loads(audit.read_text(encoding='utf-8'))['status'] != 'complete':
                raise ValueError('Both development report audits must finish before validation')
    cases, manifest = load_cases()
    cases = [c for c in cases if c['partition']==args.partition]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    suffix = '' if args.revision == 1 else '_revision2'
    directory = ROOT/'evidence/v2_campaigns'/(stamp+'_'+args.partition+suffix)
    register = ROOT/f'evidence/v2_{args.partition}{suffix}_registration.json'
    directory.parent.mkdir(parents=True,exist_ok=True)
    with register.open('x',encoding='utf-8') as f:
        json.dump({'status':'registered','directory':str(directory),'started_at':stamp},f,indent=2)
    directory.mkdir()
    paths = method_files()
    signatures = {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    config = {'started_at':stamp,'partition':args.partition,'development_revision':args.revision,
        'model':MODEL,'policy':BASELINE,'topology':'multi',
        'case_ids':[c['id'] for c in cases],'case_manifest_sha256':manifest['sha256'],
        'code_sha256':signatures,'workers':2,'arm_order':list(ARMS),
        'python_dependencies':{n:importlib.metadata.version(n) for n in ('tau2','pydantic','commerce-common','shopping-agent-core')},
        'scope':'Fresh-product, shared-template custom-domain experiment. No real users. Failed runs are never replaced.'}
    save(directory/'config.json',config)
    for path in paths:
        saved = directory/'code_snapshot'/path.relative_to(ROOT)
        saved.parent.mkdir(parents=True,exist_ok=True)
        saved.write_bytes(path.read_bytes())
    for arm in ARMS:
        (directory/arm).mkdir()
        save(directory/arm/'config.json',{**config,'arm':arm})
    rows = {arm:[] for arm in ARMS}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_case,c,directory/arm,arm):(c,arm) for c in cases for arm in ARMS}
        for future in as_completed(futures):
            case,arm = futures[future]
            try:
                row = future.result()
            except Exception as error:
                row = {'case_id':case['id'],'family':case['family'],'arm':arm,'run_status':'harness_failed',
                       'score':{'passed':False},'error':type(error).__name__+': '+str(error)[:1000]}
            rows[arm].append(row)
            save(directory/arm/'results.json',sorted(rows[arm],key=lambda r:r['case_id']))
            print(json.dumps({'completed':sum(len(v) for v in rows.values()),'total':len(cases)*2,
                'arm':arm,'case':case['id'],'passed':row['score']['passed'],'error':row.get('error')},ensure_ascii=False),flush=True)
    summaries = {}
    for arm, values in rows.items():
        latencies = [r['latency_seconds'] for r in values if 'latency_seconds' in r]
        summaries[arm] = {'cases':len(values),'passed':sum(r['score']['passed'] for r in values),
            'run_statuses':dict(Counter(r['run_status'] for r in values)),
            'settled_cost_cny':str(sum((Decimal(r.get('settled_cost_cny','0')) for r in values),Decimal(0))),
            'latency_median_seconds':statistics.median(latencies) if latencies else None}
        save(directory/arm/'summary.json',summaries[arm])
    summary = {'directory':str(directory),'partition':args.partition,'arms':summaries,
               'global_budget':BudgetedChatClient().ledger.summary()}
    save(directory/'summary.json',summary)
    save(register,{'status':'complete','directory':str(directory),'completed_at':datetime.now(timezone.utc).isoformat()})
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()

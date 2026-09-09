"""Run real budgeted app agents and external state scoring; never replace failed cases."""
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

from commerce_lab.agent import CommerceAgent
from evaluation.environment import business_snapshot, create_case_store
from evaluation.tau_bridge import evaluate_record
from research.model_client import BudgetedChatClient
from research.model_config import ROOT


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def run_case(case, directory, model, policy, topology='multi'):
    case_dir = directory / case['id']
    case_dir.mkdir()
    store, sid = create_case_store(case, case_dir / 'live.sqlite')
    dump(case_dir / 'initial_state.json', business_snapshot(store, sid))
    agent = CommerceAgent(store=store, model=model, policy=policy, topology=topology)
    start = time.monotonic()
    result = asyncio.run(agent.run(sid, case['task']))
    elapsed = time.monotonic() - start
    record = store.run(sid, result['run_id'])
    record.pop('session_id', None)
    snapshot = business_snapshot(store, sid, agent.last_quote)
    dump(case_dir / 'actual_run.json', record)
    dump(case_dir / 'live_state.json', snapshot)
    try:
        score = evaluate_record(case, record, snapshot, case_dir / 'external_score', policy=policy)
    except Exception as error:
        # Evaluation errors are explicit failures of this experiment, never successes
        # or silently discarded cases. Raw trace remains available for investigation.
        score = {'passed': False, 'evaluation_error_type': type(error).__name__, 'evaluation_error': str(error)[:1600]}
        dump(case_dir / 'grading_error.json', score)
    return {'case_id': case['id'], 'family': case['family'], 'model': model, 'policy_id': policy['id'],
        'run_status': record['status'], 'score': score, 'latency_seconds': round(elapsed,3),
        'model_calls': result['model_calls'],
        'settled_cost_cny': result.get('estimated_cost_cny', result.get('estimated_settled_cost_cny', '0')),
        'error': result.get('error')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--partition', choices=['development','validation','test'], default='development')
    parser.add_argument('--model', default='gpt-5.6-luna')
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, choices=[1,2], default=2)
    parser.add_argument('--topology', choices=['multi','single'], default='multi')
    args = parser.parse_args()
    if args.partition == 'test':
        raise SystemExit('Final evaluation remains sealed until methods and gates are implemented and frozen.')
    raw = (ROOT / 'data/commerce_cases_v1.json').read_bytes().replace(b'\r\n', b'\n')
    manifest = json.loads((ROOT / 'evidence/commerce_cases_manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256(raw).hexdigest() != manifest['sha256']:
        raise ValueError('Case manifest integrity mismatch')
    cases = [c for c in json.loads(raw)['cases'] if c['partition'] == args.partition]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError('Positive limit required')
        cases = cases[:args.limit]
    policy = json.loads(args.policy.read_text(encoding='utf-8')) if args.policy else {
        'id': 'baseline-v1', 'search_match_mode': 'all', 'retry_empty_search': False}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = ROOT / 'evidence/campaigns' / (stamp + '_' + args.partition)
    directory.mkdir(parents=True)
    files = [p for folder in ('commerce_lab','evaluation','research','logistics_lab') for p in (ROOT / folder).glob('*.py')]
    for source in files:
        saved = directory / 'code_snapshot' / source.relative_to(ROOT)
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(source.read_bytes())
    config = {'started_at': stamp, 'partition': args.partition, 'model': args.model, 'policy': policy, 'topology': args.topology,
        'case_ids': [c['id'] for c in cases], 'workers': args.workers, 'case_manifest_sha256': manifest['sha256'],
        'python_dependencies': {n: importlib.metadata.version(n) for n in ['tau2','pydantic','commerce-common','shopping-agent-core']},
        'code_sha256': {str(p.relative_to(ROOT)).replace('\\','/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        'scope': 'Real model calls on custom synthetic business cases over public metadata; no real customers; final test not run.'}
    dump(directory / 'config.json', config)
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case,c,directory,args.model,policy,args.topology): c for c in cases}
        for future in as_completed(futures):
            case = futures[future]
            try:
                row = future.result()
            except Exception as error:
                row = {'case_id': case['id'], 'family': case['family'], 'run_status': 'harness_failed',
                    'score': {'passed': False}, 'error': type(error).__name__ + ': ' + str(error)[:800]}
            rows.append(row)
            dump(directory / 'results.json', sorted(rows,key=lambda r:r['case_id']))
            print(json.dumps({'completed':len(rows),'total':len(cases),'case':case['id'],
                'passed':row['score']['passed'],'status':row['run_status'],'error':row.get('error')},ensure_ascii=False),flush=True)
    latencies = [r['latency_seconds'] for r in rows if 'latency_seconds' in r]
    summary = {'directory': str(directory), 'cases':len(rows),'passed':sum(r['score']['passed'] for r in rows),
        'run_statuses':dict(Counter(r['run_status'] for r in rows)),
        'grading_errors':sum('evaluation_error' in r['score'] for r in rows),
        'settled_cost_cny':str(sum((Decimal(r.get('settled_cost_cny','0')) for r in rows),Decimal(0))),
        'latency_median_seconds':statistics.median(latencies) if latencies else None,
        'global_budget':BudgetedChatClient().ledger.summary()}
    dump(directory / 'summary.json', summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    main()

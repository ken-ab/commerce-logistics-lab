"""Reservation-only recovery; all original cases and provider request bytes are retained."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import threading

from model_selection_100.prepare import ROOT, OUT as ORIGINAL, read, save, sha
from model_selection_100.calibrate import FIXTURE, model_dir
from model_selection_100.configured import decode
from model_selection_100.metrics import summarize, score_order
from model_selection_100.run import selected_queries, validate as validate_original, COUNTS, REFERENCE
from model_selection_100.native_bound_client import NativeBoundClient, REPAIRED_MODEL, REVISION
from research.budget import BudgetExceeded

OUT=ORIGINAL/'native_bound_v1'


def validate():
    validate_original()
    method=read(OUT/'method.json')
    for path,digest in method['files_sha256'].items():
        if sha(ROOT/path)!=digest:raise ValueError('Recovery registration changed: '+path)
    return method


def stage_models(stage):
    registry=read(ORIGINAL/'execution_models.json')['models']
    if stage=='screen':return registry
    file=OUT/('shortlist_selection.json' if stage=='shortlist' else 'winner_selection.json')
    selection=read(file)
    source=OUT/('screen_summary.json' if stage=='shortlist' else 'shortlist_summary.json')
    if selection['method_sha256']!=sha(OUT/'method.json') or selection['source_summary_sha256']!=sha(source):
        raise ValueError('Stage selection or its supporting summary changed')
    names=selection['models']
    return [m for m in registry if m['id'] in names]


def run_stage(stage,rounds=None):
    validate()
    client=NativeBoundClient();models=stage_models(stage)
    calibration={r['model_id']:r['compatible'] for r in read(ORIGINAL/'calibration_final.json')['results']}
    queries=selected_queries(stage)
    if rounds is not None:queries=queries[:rounds]
    blocked=set();lock=threading.Lock()
    # A permanent protocol/access error remains stopped across process restarts.
    for model in models:
        prior=(OUT/'results'/stage/model_dir(model['id'])).glob('*.json')
        if any(read(p).get('response',{}).get('http_status') in (400,401,403,404)
               for p in prior if not p.name.endswith('.started.json')):
            blocked.add(model['id'])
    def work(model,q):
        mid=model['id'];key=f"{q['locale']}_{q['query_id']}"
        path=OUT/'results'/stage/model_dir(mid)/(key+'.json')
        if path.exists():return read(path)
        with lock:
            if mid in blocked:return None
        if not calibration[mid]:return None
        row=read(ORIGINAL/'baseline'/stage/(key+'.json'))
        purpose=f'model-selection-100:{stage}:{mid}:{key}'
        result={'stage':stage,'model_id':mid,'query_id':q['query_id'],'locale':q['locale'],
                'query_group_sha256':q['query_group_sha256'],'submitted':True,'valid_response':False,
                'metrics':score_order(row),'pipeline_metrics':score_order(row)}
        with closing(client.ledger.connect()) as db:
            old=db.execute('SELECT id,charged,reserved FROM calls WHERE purpose=?',(purpose,)).fetchone()
        if old:
            result.update(status='interrupted_without_result',response={'budget_call_id':old[0],'api_success':False,
               'latency_seconds':None,'accounted_and_reserved_cny':str(Decimal(old[1] if old[1] is not None else old[2])/1_000_000)})
            save(path,result);return result
        try:
            response=client.chat(mid,row['model_input'],purpose=purpose,reservation_record=path.with_suffix('.started.json'))
        except BudgetExceeded:
            with lock:blocked.add(mid)
            return None
        result['response']=response;result['status']=response['status']
        if response.get('api_success') and response.get('status')!='output_limit_not_enforced':
            try:
                order=decode(model,response,[p['id'] for p in row['model_input']['candidates']])
                result.update(order=order,valid_response=True,status='valid',metrics=score_order(row,order),pipeline_metrics=score_order(row,order))
            except (ValueError,TypeError,KeyError):result['status']='invalid_ranking_format'
        if response.get('http_status') in (400,401,403,404):
            with lock:blocked.add(mid)
        save(path,result);return result
    jobs=[(m,q) for q in queries for m in sorted(models,key=lambda m:hashlib.sha256((stage+q['query_group_sha256']+m['id']).encode()).hexdigest())]
    processed=0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(work,m,q) for m,q in jobs]
        for f in as_completed(futures):
            f.result();processed+=1
            if processed%25==0:
                progress={'stage':stage,'processed_in_requested_batch':processed,'scheduled_in_batch':len(jobs),
                          'durable_result_count':len([p for p in (OUT/'results'/stage).rglob('*.json') if not p.name.endswith('.started.json')]),
                          'last_update':datetime.now(timezone.utc).isoformat(),'budget':client.ledger.summary()}
                save(OUT/(stage+'_progress.json'),progress)
                print({'stage':stage,'processed':processed,'scheduled':len(jobs),
                       'durable_results':progress['durable_result_count'],
                       'project_accounted_cny':progress['budget']['accounted_and_reserved_cny']},flush=True)
    report(stage)


def reconcile_accounting(row):
    # Derived readout only: preserve raw files and use the settled ledger where the old client lost usage.
    import copy,json,sqlite3
    row=copy.deepcopy(row);response=row['response']
    with closing(sqlite3.connect(ROOT/'evidence/api_budget.sqlite')) as db:
        record=db.execute('SELECT charged,usage FROM calls WHERE id=?',(response['budget_call_id'],)).fetchone()
    if record and record[0] is not None and response.get('estimated_cost_cny') is None:
        response['estimated_cost_cny']=response['accounted_and_reserved_cny']=str(Decimal(record[0])/1_000_000)
        response['usage']=json.loads(record[1])
        response['accounting_reconciliation']='Settled ledger overlay; original response file unchanged'
    return row


def report(stage):
    expected=COUNTS[stage]
    result={}
    for model in stage_models(stage):
        paths=(OUT/'results'/stage/model_dir(model['id'])).glob('*.json')
        rows=[reconcile_accounting(read(p)) for p in paths if not p.name.endswith('.started.json')]
        result[model['id']]=summarize(rows,expected)
    save(OUT/(stage+'_summary.json'),{'stage':stage,'created_at':datetime.now(timezone.utc).isoformat(),'models':result,
          'method_sha256':sha(OUT/'method.json'),'partial_results_are_not_full_rankings':True})
    return result


def choose(stage):
    if stage not in ('screen','shortlist'):raise ValueError('Validation cannot select another candidate')
    validate();groups=report(stage);ref=groups[REFERENCE]
    if not ref.get('complete') or ref.get('overall_score') is None:raise ValueError('Reference is incomplete')
    minvalid,ndcg_margin,acc_margin=(23,.04,2/24) if stage=='screen' else (59,.02,2/60)
    eligible=[m for m,r in groups.items() if r.get('complete') and r.get('overall_score') is not None
         and r['valid_responses']>=minvalid and r['identity_match_rate']==1
         and r['ndcg_at_10']>=ref['ndcg_at_10']-ndcg_margin and r['accuracy']>=ref['accuracy']-acc_margin]
    ranked=sorted(eligible,key=lambda m:(-groups[m]['overall_score'],-groups[m]['ndcg_at_10'],groups[m]['avg_cost_cny'],m))
    if stage=='screen':
        selected=[m for m in ranked if m!=REFERENCE][:5]+[REFERENCE]
        target=OUT/'shortlist_selection.json'
    else:
        if not ranked:raise ValueError('No eligible candidate; retain current system')
        selected=list(dict.fromkeys([ranked[0],REFERENCE]));target=OUT/'winner_selection.json'
    if target.exists():raise ValueError('Selection already frozen')
    save(target,{'models':selected,'winner':ranked[0] if stage=='shortlist' else None,
                'eligible':ranked,'created_at':datetime.now(timezone.utc).isoformat(),
                'source_summary_sha256':sha(OUT/(stage+'_summary.json')),'method_sha256':sha(OUT/'method.json'),
                'validation_outcomes_used':False})
    print({'stage':stage,'selected':selected},flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['screen','shortlist','validation','choose-screen','choose-shortlist','report'])
    parser.add_argument('--rounds',type=int);parser.add_argument('--stage',default='screen')
    args=parser.parse_args()
    if args.action.startswith('choose-'):choose(args.action[7:])
    elif args.action=='report':report(args.stage)
    else:run_stage(args.action,args.rounds)

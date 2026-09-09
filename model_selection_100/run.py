"""Resumable staged selection; never use validation to choose a model."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import threading

from model_selection_100.prepare import ROOT,OUT,read,save,sha
from model_selection_100.calibrate import FIXTURE,model_dir
from model_selection_100.configured import ConfiguredClient,decode
from model_selection_100.metrics import summarize,score_order
from research.budget import BudgetExceeded

REFERENCE='gpt-5.6-luna'
COUNTS={'screen':24,'shortlist':60,'validation':150}


def compatible_calibration():
    client=ConfiguredClient()
    previous={r['model_id']:r for r in read(OUT/'calibration_summary.json')['results']}
    registration=OUT/'calibration_adaptation_registration.json'
    bound=['model_selection_100/configured.py','model_selection_100/client.py',
           'evidence/model_selection_100/execution_models.json','model_selection_100/budget.py',
           'model_selection_100/budget_policy.json']
    hashes={p:sha(ROOT/p) for p in bound}
    if registration.exists():
        if read(registration)['files_sha256']!=hashes:raise ValueError('Calibration adaptation changed')
    else:save(registration,{'created_at':datetime.now(timezone.utc).isoformat(),'files_sha256':hashes,
                           'basis':'Protocol errors/identity names only; no ranking benchmark outcomes observed'})
    rows=[]
    def work(model):
        mid=model['id'];path=OUT/'calibration_adapted'/model_dir(mid)/'result.json'
        if path.exists():return read(path)
        old=previous.get(mid)
        if old and not model.get('configuration_reason'):
            response=old['response']
            if response.get('returned_model') in model.get('accepted_returned_aliases',[]):
                response=dict(response,identity_match=True)
            reused=True
        else:
            response=client.chat(mid,FIXTURE,purpose='model-selection-100:calibration-adapted:'+mid,
                                 reservation_record=path.with_name('started.json'))
            reused=False
        valid=False
        if response.get('api_success'):
            try:decode(model,response,['c01','c02']);valid=True
            except (ValueError,TypeError,KeyError):pass
        item={'model_id':mid,'compatible':bool(valid and response.get('identity_match')),
              'valid_format':valid,'response':response,'reused_original_calibration':reused}
        save(path,item);return item
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(work,m) for m in client.registry['models']]
        for future in as_completed(futures):
            item=future.result();rows.append(item)
            if not item['reused_original_calibration']:
                print({'model':item['model_id'],'compatible':item['compatible'],'returned':item['response'].get('returned_model')},flush=True)
    save(OUT/'calibration_final.json',{'status':'complete','models':len(rows),'compatible':sum(r['compatible'] for r in rows),
         'results':sorted(rows,key=lambda r:r['model_id']),'budget':client.ledger.summary(),
         'created_at':datetime.now(timezone.utc).isoformat()})
    print({'models':len(rows),'compatible':sum(r['compatible'] for r in rows)},flush=True)


def freeze():
    target=OUT/'method.json'
    if target.exists():return validate()
    baseline=read(OUT/'baseline_registration.json')
    if baseline['status']!='complete' or baseline['queries']!=480:raise ValueError('Baseline not complete')
    if not (OUT/'calibration_final.json').exists():raise ValueError('Protocol calibration not complete')
    calibration=read(OUT/'calibration_final.json')
    names={m['id'] for m in read(OUT/'execution_models.json')['models']}
    if len(names)!=100 or {r['model_id'] for r in calibration['results']}!=names or calibration['compatible']!=100:
        raise ValueError('Final 100-model registry has not passed compatibility review')
    groups=[q['query_group_sha256'] for s in COUNTS for q in selected_queries(s)]
    if len(groups)!=234 or len(set(groups))!=234:raise ValueError('Active query groups overlap or are incomplete')
    with closing(__import__('sqlite3').connect(ROOT/'evidence/api_budget.sqlite')) as db:
        count=db.execute("SELECT COUNT(*) FROM calls WHERE purpose LIKE 'model-selection-100:screen:%' "
                         "OR purpose LIKE 'model-selection-100:shortlist:%' OR purpose LIKE 'model-selection-100:validation:%'").fetchone()[0]
    if count:raise ValueError('Cannot claim a prospective freeze after benchmark calls')
    files=[p for p in (ROOT/'model_selection_100').glob('*.py') if p.name!='forecast.py']
    files += [ROOT/p for p in ['model_selection_100/PROTOCOL.md','model_selection_100/budget_policy.json',
        'research/budget.py','research/model_config.py','research/model_client.py','research/tls_transport.py',
        'ranking_compare/experiment.py','ranking/metrics.py','ranking/model.py','ranking/evaluate.py',
        'evidence/model_selection_100/selection.json','evidence/model_selection_100/baseline_registration.json',
        'evidence/model_selection_100/execution_models.json','evidence/model_selection_100/calibration_final.json']]
    for p,h in baseline['files_sha256'].items():
        if sha(ROOT/p)!=h:raise ValueError('Baseline changed: '+p)
        files.append(ROOT/p)
    record={'created_at':datetime.now(timezone.utc).isoformat(),'status':'frozen_before_screen_quality_calls',
            'files_sha256':{str(p.relative_to(ROOT)):sha(p) for p in sorted(set(files))},
            'stages':COUNTS,'maximum_workers':4,'user_selection_task_limit_cny':100,
            'user_whole_project_limit_cny':480,'validation_not_used_for_selection':True}
    save(target,record);return record


def validate():
    method=read(OUT/'method.json')
    for p,h in method['files_sha256'].items():
        if sha(ROOT/p)!=h:raise ValueError('Frozen selection method changed: '+p)
    return method


def selected_queries(stage):
    queries=[q for q in read(OUT/'selection.json')['queries'] if q['stage']==stage]
    buckets={loc:sorted((q for q in queries if q['locale']==loc),key=lambda q:q['selection_sha256']) for loc in ('us','es','jp')}
    return [buckets[loc][i] for i in range(len(buckets['us'])) for loc in ('us','es','jp')][:COUNTS[stage]]


def stage_models(stage):
    registry=read(OUT/'execution_models.json')['models']
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
    client=ConfiguredClient();models=stage_models(stage)
    calibration={r['model_id']:r['compatible'] for r in read(OUT/'calibration_final.json')['results']}
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
        row=read(OUT/'baseline'/stage/(key+'.json'))
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
        if response.get('api_success'):
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
                          'last_update':datetime.now(timezone.utc).isoformat(),'budget':client.ledger.summary()}
                save(OUT/(stage+'_progress.json'),progress)
                print({'stage':stage,'processed':processed,'scheduled':len(jobs),
                       'project_accounted_cny':progress['budget']['accounted_and_reserved_cny']},flush=True)
    report(stage)


def report(stage):
    expected=COUNTS[stage]
    result={}
    for model in stage_models(stage):
        paths=(OUT/'results'/stage/model_dir(model['id'])).glob('*.json')
        rows=[read(p) for p in paths if not p.name.endswith('.started.json')]
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
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['calibrate-final','freeze','screen','shortlist','validation','choose-screen','choose-shortlist','report'])
    parser.add_argument('--rounds',type=int);parser.add_argument('--stage',default='screen')
    args=parser.parse_args()
    if args.action=='calibrate-final':compatible_calibration()
    elif args.action=='freeze':print({'frozen_files':len(freeze()['files_sha256'])})
    elif args.action.startswith('choose-'):choose(args.action[7:])
    elif args.action=='report':report(args.stage)
    else:run_stage(args.action,args.rounds)

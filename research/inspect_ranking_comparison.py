"""Read-only post-run accounting and source verification for the small ranking study."""
from collections import Counter
from contextlib import closing
from decimal import Decimal
from datetime import datetime,timezone
import json
import sqlite3

from ranking_compare.experiment import ROOT,OUT,PREFIX,read,sha,save,validate_method,decode_order,score_order


def inspect():
    validate_method()
    if read(OUT/'progress.json')['status']!='complete':
        raise ValueError('Small comparison has not finished')
    development=read(OUT/'development_summary.json')
    models=development['active_models'];selected=development['selected_model']
    expected={(stage,model,count) for stage,candidates,count in
              [('development',models,24),('validation',[selected] if selected else [],48)] for model in candidates}
    source_hashes={};groups={};seen_budget_ids=set()
    with closing(sqlite3.connect(ROOT/'evidence/api_budget.sqlite')) as db:
        for stage,model,count in sorted(expected):
            paths=sorted((OUT/stage/model).glob('*/result.json'))
            if len(paths)!=count:raise ValueError('Incomplete or excess model/case results')
            results=[]
            for path in paths:
                r=read(path);source=ROOT/r['source'];baseline=read(source)
                if sha(source)!=r['source_sha256']:raise ValueError('Changed query source')
                if r['stage']!=stage or r['model']!=model:raise ValueError('Wrong case model/stage')
                if r['status']=='valid':
                    order=decode_order(r['response'],{c['id'] for c in baseline['model_input']['candidates']})
                    if order!=r['order'] or score_order(baseline,order)!=r['metrics']:
                        raise ValueError('Stored score differs from original complete response')
                elif score_order(baseline)!=r['metrics']:
                    raise ValueError('Failed response did not preserve baseline fallback')
                if r.get('response'):
                    call_id=r['response']['budget_call_id']
                    if call_id in seen_budget_ids:raise ValueError('A provider call was counted more than once')
                    seen_budget_ids.add(call_id)
                    call=db.execute('SELECT purpose,model,status FROM calls WHERE id=?',(call_id,)).fetchone()
                    if not call or call[0]!=PREFIX+stage+':'+model+':'+source.stem or call[1]!=model or call[2]!='settled':
                        raise ValueError('Returned response is not bound to its settled shared-ledger call')
                source_hashes[str(path.relative_to(ROOT))]=sha(path);results.append(r)
            if len({r['query_group_sha256'] for r in results})!=count:raise ValueError('Duplicate query group')
            changes=[r['metrics']['ndcg_at_10']-r['baseline_metrics']['ndcg_at_10'] for r in results]
            groups[stage+':'+model]={'queries':count,'statuses':dict(Counter(r['status'] for r in results)),
                'ndcg_better':sum(d>1e-12 for d in changes),'ndcg_equal':sum(abs(d)<=1e-12 for d in changes),
                'ndcg_worse':sum(d<-1e-12 for d in changes),
                'returned_model_ids':dict(Counter(r.get('response',{}).get('returned_model') for r in results)),
                'errors':dict(Counter(r.get('error_type','not_submitted_or_interrupted') for r in results if r['status']!='valid'))}
        costs=db.execute('SELECT model,status,COUNT(*),SUM(COALESCE(charged,reserved)) FROM calls WHERE purpose LIKE ? GROUP BY model,status',(PREFIX+'%',)).fetchall()
    accounting=[{'model':m,'status':s,'calls':n,'accounted_and_reserved_cny':str(Decimal(amount)/1_000_000)} for m,s,n,amount in costs]
    for path in (OUT/'method.json',OUT/'progress.json',OUT/'development_summary.json',OUT/'calibration.json'):
        source_hashes[str(path.relative_to(ROOT))]=sha(path)
    if selected:source_hashes[str((OUT/'validation_summary.json').relative_to(ROOT))]=sha(OUT/'validation_summary.json')
    result={'status':'verified','created_at':datetime.now(timezone.utc).isoformat(),'groups':groups,
        'accounting':accounting,'total_accounted_and_reserved_cny':str(sum(Decimal(r['accounted_and_reserved_cny']) for r in accounting)),
        'note':'Includes calibration and uncertain call reservations; conservative local estimates, not provider invoices.',
        'sources_sha256':source_hashes}
    save(OUT/'integrity_and_accounting.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='sources_sha256'},ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':inspect()

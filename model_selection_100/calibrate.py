"""Compatibility-only fixed fixture; no benchmark labels or quality tuning."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib

from model_selection_100.prepare import ROOT,OUT,read,save,sha
from model_selection_100.client import SelectionClient
from model_selection_100.metrics import decode_order
from research.budget import BudgetExceeded

FIXTURE={'query':'blue cotton shirt','candidates':[
    {'id':'c01','text':'Title: Red ceramic dinner plate. Brand: Fixture. Color: Red.'},
    {'id':'c02','text':'Title: Blue cotton shirt. Brand: Fixture. Color: Blue.'}]}


def model_dir(model):return hashlib.sha256(model.encode()).hexdigest()[:16]


def main():
    registration=OUT/'calibration_registration.json'
    files=['model_selection_100/calibrate.py','model_selection_100/client.py','model_selection_100/metrics.py',
           'model_selection_100/budget.py','model_selection_100/budget_policy.json','model_selection_100/PROTOCOL.md',
           'research/model_client.py','research/model_config.py','research/budget.py','research/tls_transport.py',
           'evidence/model_selection_100/candidates.json']
    hashes={p:sha(ROOT/p) for p in files}
    if registration.exists():
        if read(registration)['files_sha256']!=hashes:raise ValueError('Calibration code/config changed; record an explicit revision')
    else:save(registration,{'created_at':datetime.now(timezone.utc).isoformat(),'files_sha256':hashes,
                           'fixture':FIXTURE,'purpose':'protocol compatibility only','maximum_workers':4})
    client=SelectionClient()
    models=client.registry['models']
    def work(model):
        mid=model['id'];directory=OUT/'calibration'/model_dir(mid);target=directory/'result.json'
        if target.exists():return read(target)
        purpose='model-selection-100:calibration:'+mid
        with closing(client.ledger.connect()) as db:
            old=db.execute('SELECT id,status FROM calls WHERE purpose=?',(purpose,)).fetchone()
        if old:
            result={'model_id':mid,'compatible':False,'status':'interrupted_existing_reservation',
                    'budget_call_id':old[0],'no_resubmission':True}
            save(target,result);return result
        try:
            response=client.chat(mid,FIXTURE,purpose=purpose,reservation_record=directory/'started.json')
        except BudgetExceeded:
            return {'model_id':mid,'compatible':False,'status':'not_submitted_budget'}
        valid=False
        if response.get('api_success'):
            try:
                decode_order(response['message'],['c01','c02'],response.get('finish_reason'))
                valid=True
            except (ValueError,TypeError,KeyError):pass
        result={'model_id':mid,'compatible':valid and response.get('identity_match',False),
                'valid_format':valid,'identity_match':response.get('identity_match',False),'response':response,
                'status':'compatible' if valid and response.get('identity_match') else 'needs_review'}
        save(target,result);return result
    complete=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(work,m):m['id'] for m in models}
        for future in as_completed(futures):
            r=future.result();complete.append(r)
            print({'calibrated':len(complete),'total':100,'model':r['model_id'],'status':r['status']},flush=True)
            save(OUT/'calibration_progress.json',{'status':'running','completed':len(complete),'total':100,
                 'compatible':sum(x.get('compatible',False) for x in complete)})
    save(OUT/'calibration_summary.json',{'status':'complete','created_at':datetime.now(timezone.utc).isoformat(),
         'models':len(complete),'compatible':sum(x.get('compatible',False) for x in complete),
         'results':sorted(complete,key=lambda x:x['model_id']),'budget':client.ledger.summary(),
         'no_quality_benchmark_calls':True})
    print({'status':'complete','compatible':sum(x.get('compatible',False) for x in complete),'total':len(complete)},flush=True)


if __name__=='__main__':main()

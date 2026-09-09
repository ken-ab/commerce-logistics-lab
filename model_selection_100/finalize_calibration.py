"""Finalize reviewed aliases and one replacement under the new CNY 100 task cap."""
from datetime import datetime,timezone
from contextlib import closing
from copy import deepcopy

from model_selection_100.prepare import ROOT,OUT,read,save,sha
from model_selection_100.configured import ConfiguredClient,decode
from model_selection_100.client import identity_key
from model_selection_100.calibrate import FIXTURE,model_dir


def main():
    if (OUT/'method.json').exists():raise ValueError('No calibration changes after quality methods freeze')
    path=OUT/'execution_models.json';registry=read(path)
    for model in registry['models']:
        if model['id']=='doubao-seed-2-0-pro':
            model['accepted_returned_aliases']=['doubao-seed-2-0-pro-260215']
            model['alias_source']='https://aihubmix.com/model/doubao-seed-2-0-pro'
    save(path,registry)
    client=ConfiguredClient()
    before=OUT/'calibration_final_before_100cny_revision.json'
    previous={r['model_id']:r for r in read(before)['results']}
    registration=OUT/'calibration_budget100_registration.json'
    files=['model_selection_100/finalize_calibration.py','model_selection_100/client.py','model_selection_100/configured.py',
           'model_selection_100/budget.py','model_selection_100/budget_policy.json','evidence/model_selection_100/execution_models.json']
    hashes={p:sha(ROOT/p) for p in files}
    if registration.exists():
        if read(registration)['files_sha256']!=hashes:raise ValueError('Compatibility registration changed')
    else:save(registration,{'created_at':datetime.now(timezone.utc).isoformat(),'files_sha256':hashes,
                           'source_calibration_sha256':sha(before),'no_benchmark_outcomes_used':True})
    results=[]
    for model in registry['models']:
        mid=model['id'];target=OUT/'calibration_budget100'/model_dir(mid)/'result.json'
        if target.exists():
            results.append(read(target));continue
        if mid in previous:
            response=deepcopy(previous[mid]['response']);reused=True
        else:
            purpose='model-selection-100:calibration-budget100:'+mid
            with closing(client.ledger.connect()) as db:
                if db.execute('SELECT 1 FROM calls WHERE purpose=?',(purpose,)).fetchone():
                    raise ValueError('Existing paid request has no durable result; no duplicate submission')
            response=client.chat(mid,FIXTURE,purpose=purpose,reservation_record=target.with_name('started.json'))
            reused=False
        response['identity_match']=(identity_key(response.get('returned_model'))==identity_key(mid)
                                     or response.get('returned_model') in model.get('accepted_returned_aliases',[]))
        valid=False
        if response.get('api_success'):
            try:decode(model,response,['c01','c02']);valid=True
            except (ValueError,TypeError,KeyError):pass
        result={'model_id':mid,'compatible':bool(valid and response['identity_match']),
                'valid_format':valid,'response':response,'reused_previous_calibration':reused}
        save(target,result);results.append(result)
        if not reused:print({'model':mid,'compatible':result['compatible'],'returned':response.get('returned_model'),
                              'http':response.get('http_status')},flush=True)
    save(OUT/'calibration_final.json',{'status':'complete','models':len(results),'compatible':sum(r['compatible'] for r in results),
         'results':results,'budget':client.ledger.summary(),'created_at':datetime.now(timezone.utc).isoformat(),
         'revision_registration_sha256':sha(registration),'previous_results_preserved':str(before.relative_to(ROOT))})
    print({'final_models':len(results),'compatible':sum(r['compatible'] for r in results)},flush=True)


if __name__=='__main__':main()

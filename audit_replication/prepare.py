"""Register disjoint products once, without looking at new model outcomes."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import heapq
import json

from commerce_lab.catalog import Catalog
from evaluation.prepare_cases import make_cases
from evaluation.business_freeze import read, sha
from evaluation.run_final import save
from evaluation_v2.prepare import prior_product_ids, chinese_task
from research.model_config import ROOT

DATA = ROOT/'data/commerce_cases_audit_replication.json'
MANIFEST = ROOT/'evidence/audit_replication_manifest.json'
SALT = 'audit-transport-independent-replication-20260908:'


def collect_ids(value, target):
    if isinstance(value,dict):
        for key,item in value.items():
            if key=='product_id' and isinstance(item,str):
                target.add(item)
            elif key=='product_ids' and isinstance(item,list):
                target.update(v for v in item if isinstance(v,str))
            else:
                collect_ids(item,target)
    elif isinstance(value,list):
        for item in value:
            collect_ids(item,target)


def main():
    if DATA.exists() or MANIFEST.exists():
        raise ValueError('The independent replication dataset already exists')
    excluded, prior_count = prior_product_ids()
    collect_ids(read(ROOT/'data/commerce_cases_v2.json'),excluded)
    files = list((ROOT/'evidence/v2_campaigns').glob('**/actual_run.json'))
    for path in files:
        collect_ids(read(path),excluded)
    catalog = Catalog()
    with closing(catalog.connect()) as db:
        candidates = [r[0] for r in db.execute(
            "SELECT id FROM products WHERE locale='us' AND lower(title) LIKE '%shirt%' AND length(description)>40")
            if r[0] not in excluded]
    selected = heapq.nsmallest(24,candidates,key=lambda value:hashlib.sha256((SALT+value).encode()).hexdigest())
    if len(set(selected))!=24:
        raise ValueError('Insufficient disjoint product groups')
    cases = []
    cursor = 0
    for partition,count in (('validation',4),('test',20)):
        for ordinal,ident in enumerate(selected[cursor:cursor+count]):
            for case in make_cases(ident,partition,ordinal):
                case['id'] = 'replication-'+case['id']
                case['response_language'] = 'zh' if ordinal%2 else 'en'
                if case['response_language']=='zh':
                    case['task'] = chinese_task(case,ordinal)
                cases.append(case)
        cursor += count
    payload = {'version':'audit-transport-independent-replication-1','cases':cases,
               'source_records':{ident:catalog.get(ident) for ident in selected}}
    with DATA.open('x',encoding='utf-8',newline='\n') as handle:
        handle.write(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    manifest = {'file':DATA.relative_to(ROOT).as_posix(),'sha256':sha(DATA),'selection_salt':SALT,
        'registered_at':datetime.now(timezone.utc).isoformat(),'excluded_product_ids':sorted(excluded),
        'prior_business_trace_count':prior_count+len(files),'groups':{'validation':4,'test':20},
        'case_counts':{'validation':32,'test':160},'prior_product_overlap':len(set(selected)&excluded),
        'cross_partition_product_overlap':0,'scope':'Fresh products, shared known templates, simulated fields; zero real users.'}
    with MANIFEST.open('x',encoding='utf-8') as handle:
        json.dump(manifest,handle,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in manifest.items() if k!='excluded_product_ids'},indent=2))


def load_cases():
    manifest = read(MANIFEST)
    if sha(DATA)!=manifest['sha256']:
        raise ValueError('Registered replication data changed')
    payload = read(DATA)
    catalog = Catalog()
    if any(catalog.get(ident)!=item for ident,item in payload['source_records'].items()):
        raise ValueError('Registered source records changed')
    cases = payload['cases']
    if len(cases)!=192 or len({c['id'] for c in cases})!=192:
        raise ValueError('Replication cases are missing or duplicated')
    return cases,manifest


if __name__=='__main__':
    main()

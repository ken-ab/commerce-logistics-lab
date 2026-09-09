"""Freeze and run v4/v5 paired validation on new states in known scenario families."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import argparse
import json
from pathlib import Path
import random
import statistics

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_candidate_v4 import CandidateSearchAgent
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import instant, iso
from research.apparel_candidate_validation import (OWNER, FAMILIES, cases as old_cases, clone, eligible,
    evaluate, fixture_check, read, save, setup, sha)
from research.apparel_candidate_integration import ledger
from research.provider_gate import ProviderGate, guarded_business_client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'evidence/apparel_source_validation_v1'
MODES = ('v4_candidate', 'v5_source_review')


def cases():
    rows = old_cases()
    targets = ('us:B06XW9N3S5', 'us:B06XWPMG43')
    for case in rows:
        variation, family = case['variation'], case['family']
        case['id'] = case['id'].replace('CV-', 'SR-')
        sku = targets[variation]; variant = case['world']['variants'][sku]
        for stock in case['world']['stock'].values(): stock['available_catalog_units']=120
        if family in ('shortage_alternative','approved_revision'):
            case['world']['stock'][sku]['available_catalog_units']=0
        if family=='aggregate_stock':case['world']['stock'][sku]['available_catalog_units']=30
        request=case['request']
        for line in request['lines']:
            for field in ('brand','color','size','category'):line[field]=variant[field]
            if 'requested_sku' in line:line['requested_sku']=sku
            if 'style_id' in line:line['style_id']=variant['style_id']
            line['quantity']=26 if variation==0 else 34
            if family=='no_exact':line['brand']='Absent Commerce Research' if variation==0 else 'Missing Source Lab'
            if family=='constraint_conflict':line['size' if variation==0 else 'brand']='XS' if variation==0 else 'Goodthreads'
            if family=='unit_problem' and variation==1:line['quantity']=33
            if family=='policy_block' and variation==1:line['quantity']=4
            if family=='aggregate_stock':line['quantity']=20
        for selection in case['initial_selections']:selection['sku']=sku
        case['now']=iso(instant(case['now'])+timedelta(days=68))
        if request['needs_shipping']:
            shipping=request['shipping']
            for field in ('ready_at','deadline_at'):shipping[field]=iso(instant(shipping[field])+timedelta(days=68))
            if shipping['budget_cents']!=1:shipping['budget_cents']=32900
        expected=case['expected']
        if family in ('exact_search','shipping_new'):
            expected['allowed']={'item':eligible(request['lines'][0],request,case['world'])}
        elif family=='no_exact':expected['allowed']={}
        elif family in ('shortage_alternative','approved_revision'):
            options=eligible(request['lines'][0],request,case['world'],substitutes=True)
            assert options
            expected['allowed']={'item':options}
            if family=='approved_revision':
                case['approve_sku']=options[0]
                expected['allowed']={'item':[options[0]]}
        else:expected['allowed']={line['line_id']:[sku] for line in request['lines']}
    return rows


def current_material_check(case, execution):
    """External check of returned complete data, separate from the v5 receipt flag."""
    if not case['expected']['read_variant']:
        return {'required':False,'passed':True,'matched':{},'missing':[]}
    current=execution['after']; world=case['world']; matched={}
    required=sorted({s['sku'] for s in current['selections']})
    for sku in required:
        for trace in execution['traces']:
            if trace['kind']!='tool' or not trace['success'] or trace['tool']!='read_variant':continue
            result=trace['result']; variant=dict(result.get('variant',{}))
            if variant.pop('description_excerpt_truncated',False):continue
            if (variant==world['variants'][sku] and result.get('stock')==world['stock'][sku] and
                result.get('brand_rule')==world['brand_rules'][variant['brand']] and
                digest(variant['source_record'])==variant['source_record_sha256']):
                matched[sku]=trace['observation_id']
    missing=[sku for sku in required if sku not in matched]
    return {'required':True,'passed':not missing,'matched':matched,'missing':missing}


def prepare():
    if OUT.exists():raise FileExistsError('Preserve the existing registration')
    rows=cases();OUT.mkdir();(OUT/'runs').mkdir();(OUT/'fixtures').mkdir()
    save(OUT/'cases.json',rows)
    checks,initial_sha=[],{}
    for case in rows:
        initial=OUT/'initial'/case['id'];store,seed=setup(case,initial)
        checks.append(fixture_check(case,initial,seed,OUT/'fixtures'/(case['id']+'.sqlite')))
        for path in initial.iterdir():initial_sha[path.relative_to(OUT).as_posix()]=sha(path)
    save(OUT/'fixture_checks.json',checks)
    order=[c['id'] for c in rows];random.Random(26090961).shuffle(order)
    jobs=[(ident,mode) for index,ident in enumerate(order) for mode in (MODES if index%2==0 else MODES[::-1])]
    previous=read(ROOT/'evidence/apparel_candidate_validation_v1/registration.json')
    sources=set(previous['source_sha256']) | {'research/apparel_source_validation.py',
        'research/APPAREL_SOURCE_REVIEW_PROTOCOL.md','apparel_fulfillment/source_review.py',
        'apparel_fulfillment/agent_source_v5.py','tests/test_apparel_source_review.py'}
    save(OUT/'registration.json',{'registered_at':datetime.now(timezone.utc).isoformat(),'model':MODEL,
        'cases':24,'runs':48,'jobs':jobs,'cases_sha256':sha(OUT/'cases.json'),'fixture_sha256':sha(OUT/'fixture_checks.json'),
        'initial_sha256':initial_sha,'source_sha256':{name:sha(ROOT/name) for name in sorted(sources)},
        'scope':'New quantities, product targets and dates in known catalogue and scenario templates; not unseen merchants or products.',
        'ledger_before':ledger(),'estimated_cny':[2,5],'project_budget_cny':480,'model_selection_calls':0,'deployment_changed':False})
    print(json.dumps({'registered_runs':48,'free_fixture_checks':len(checks),'all_fixtures_passed':all(c['passed'] for c in checks)}))


def run():
    if (OUT/'summary.json').exists():raise FileExistsError('Completed results are frozen')
    reg=read(OUT/'registration.json')
    assert all(sha(ROOT/name)==value for name,value in reg['source_sha256'].items())
    assert all(sha(OUT/name)==value for name,value in reg['initial_sha256'].items())
    assert sha(OUT/'cases.json')==reg['cases_sha256'] and sha(OUT/'fixture_checks.json')==reg['fixture_sha256']
    rows={c['id']:c for c in read(OUT/'cases.json')}; records=[]
    gate=ProviderGate(ROOT/'evidence/provider_availability.sqlite')
    for ident,mode in reg['jobs']:
        folder=OUT/'runs'/(ident+'-'+mode)
        if (folder/'result.json').exists():records.append(read(folder/'result.json'));continue
        if folder.exists():raise RuntimeError('Partial attempt requires explicit recovery; no automatic duplicate')
        client=guarded_business_client(gate);client.ensure_available(MODEL)
        folder.mkdir();case=rows[ident];initial=OUT/'initial'/ident;seed=read(initial/'seed.json')
        store=clone(initial,folder/'operations.sqlite')
        assert digest(store.view(OWNER,seed['draft_id']))==seed['view_digest']
        cls=CandidateSearchAgent if mode=='v4_candidate' else SourceReviewAgent
        agent=cls(store,OWNER,seed['draft_id'],client=client,arm='single',now=instant(case['now']),
            contract=case['contract'],phase='source_validation_v1_'+mode)
        save(folder/'attempt.json',{'case_id':ident,'mode':mode,'purpose':agent.purpose,'registration_sha256':sha(OUT/'registration.json')})
        execution=agent.run(case['task']);save(folder/'execution.json',execution)
        acceptance=evaluate(case,execution,store,seed)
        material=current_material_check(case,execution)
        record={'case_id':ident,'family':case['family'],'mode':mode,'evaluation':acceptance,'current_material':material,
            'source_submission_rejections':sum(t['kind']=='source_check' and bool(t['errors']) for t in execution['traces']),
            'execution_sha256':sha(folder/'execution.json'),**{k:execution[k] for k in ('run_status','error_type','model_calls',
                'successful_model_calls','tool_calls','successful_tool_calls','input_tokens','output_tokens','accounted_and_reserved_cny','latency_seconds')}}
        save(folder/'result.json',record);records.append(record)
        print(json.dumps({'completed':len(records),'total':48,'case':ident,'mode':mode,'passed':acceptance['passed'],
            'failures':acceptance['failures'],'source_current':material['passed'],'cny':record['accounted_and_reserved_cny']},ensure_ascii=False),flush=True)
    groups={}
    for mode in MODES:
        selected=[r for r in records if r['mode']==mode];times=sorted(r['latency_seconds'] for r in selected)
        groups[mode]={'runs':len(selected),'passed':sum(r['evaluation']['passed'] for r in selected),
            'violations':sum(len(r['evaluation']['constraint_violations']) for r in selected),
            'material_failures':sum(not r['current_material']['passed'] for r in selected),
            'source_submission_rejections':sum(r['source_submission_rejections'] for r in selected),
            **{k:sum(r[k] for r in selected) for k in ('model_calls','successful_model_calls','tool_calls','successful_tool_calls','input_tokens','output_tokens')},
            'cost_cny':str(sum((Decimal(r['accounted_and_reserved_cny']) for r in selected),Decimal(0))),
            'mean_latency_seconds':statistics.mean(times),'p95_latency_seconds':times[(95*len(times)+99)//100-1],
            'failures':dict(Counter(f for r in selected for f in r['evaluation']['failures'])),
            'families':{f:{'runs':sum(r['family']==f for r in selected),'passed':sum(r['family']==f and r['evaluation']['passed'] for r in selected)} for f in FAMILIES}}
    pairs=Counter()
    for ident in rows:
        pair={r['mode']:r['evaluation']['passed'] for r in records if r['case_id']==ident}
        pairs['win' if pair[MODES[1]]>pair[MODES[0]] else 'loss' if pair[MODES[1]]<pair[MODES[0]] else 'tie']+=1
    old,new=(groups[mode] for mode in MODES)
    conditions={'minimum_completion':new['passed']>=23,'no_total_regression':new['passed']>=old['passed'],
        'no_authority_violation':new['violations']==0,'no_material_omission':new['material_failures']==0,
        'no_family_regression':all(new['families'][f]['passed']>=old['families'][f]['passed'] for f in FAMILIES),
        'mean_cost_within_1_5x':Decimal(new['cost_cny'])<=Decimal(old['cost_cny'])*Decimal('1.5')}
    save(OUT/'summary.json',{'completed_at':datetime.now(timezone.utc).isoformat(),'registration_sha256':sha(OUT/'registration.json'),
        'groups':groups,'paired_cases':dict(pairs),'engineering_gate':conditions,'engineering_gate_passed':all(conditions.values()),
        'result_sha256':{p.relative_to(OUT).as_posix():sha(p) for p in sorted((OUT/'runs').glob('*/result.json'))},
        'ledger_before':reg['ledger_before'],'ledger_after':ledger(),'new_real_users':0,'deployment_changed':False})
    print(json.dumps({k:v for k,v in read(OUT/'summary.json').items() if k!='result_sha256'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['prepare','run'])
    {'prepare':prepare,'run':run}[p.parse_args().phase]()

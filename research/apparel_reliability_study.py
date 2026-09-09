"""Paired v5/v6 validation, with all three execution policies held fixed."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import json
from pathlib import Path
import random
import statistics

from apparel_fulfillment.agent import MODEL
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.agent_reliability_v6 import ReliabilityAgent
from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import instant
from research.apparel_reliability_cases import OWNER,FAMILIES,cases,setup,fixture_check
from research.apparel_expansion_evaluation import evaluate
from research.apparel_candidate_validation import clone,read,save,sha
from research.apparel_candidate_integration import ledger
from research.provider_gate import ProviderGate,guarded_business_client

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/apparel_reliability_study_v1'
ARMS=('single','coordinator','on_demand')
VERSIONS=('v5','v6')
CONDITIONS=tuple(v+'_'+a for v in VERSIONS for a in ARMS)
SEED=26090984


def prepare():
    if OUT.exists():raise FileExistsError('Preserve registration or partial setup')
    rows=cases();assert len(rows)==24
    OUT.mkdir();(OUT/'runs').mkdir();(OUT/'fixtures').mkdir()
    save(OUT/'cases.json',rows)
    checks,hashes=[],{}
    for case in rows:
        initial=OUT/'initial'/case['id'];store,seed=setup(case,initial)
        checks.append(fixture_check(case,initial,seed,OUT/'fixtures'/(case['id']+'.sqlite')))
        hashes.update({p.relative_to(OUT).as_posix():sha(p) for p in initial.iterdir()})
    save(OUT/'fixture_checks.json',checks)
    order=[c['id'] for c in rows];random.Random(SEED).shuffle(order)
    schedules=[CONDITIONS[i:]+CONDITIONS[:i] for i in range(6)]
    schedules+=list(tuple(reversed(s)) for s in schedules)
    jobs=[(ident,condition) for i,ident in enumerate(order) for condition in schedules[i%12]]
    balance={c:[sum(s[p]==c for s in schedules)*2 for p in range(6)] for c in CONDITIONS}
    assert all(count==[4]*6 for count in balance.values())
    previous=read(ROOT/'evidence/apparel_expansion_study_v1/registration.json')
    sources=set(previous['source_sha256'])|{'apparel_fulfillment/reliability_support.py',
        'apparel_fulfillment/agent_reliability_v6.py','research/apparel_reliability_cases.py',
        'research/apparel_reliability_study.py','research/APPAREL_RELIABILITY_PROTOCOL.md',
        'tests/test_apparel_reliability.py','evidence/apparel_reliability_tests_20260909.json'}
    tests=read(ROOT/'evidence/apparel_reliability_tests_20260909.json');assert tests['passed']
    save(OUT/'registration.json',{'registered_at':datetime.now(timezone.utc).isoformat(),'cases':24,'runs':144,
        'model':MODEL,'conditions':CONDITIONS,'jobs':jobs,'seed':SEED,'condition_position_balance':balance,
        'cases_sha256':sha(OUT/'cases.json'),'fixture_sha256':sha(OUT/'fixture_checks.json'),
        'initial_sha256':hashes,'source_sha256':{name:sha(ROOT/name) for name in sorted(sources)},
        'ledger_before':ledger(),'estimated_cny':[8,25],'project_budget_cny':480,'model_selection_calls':0,
        'scope':'New developer-authored event compositions in a known historical catalogue; single stochastic trajectory per condition. Not unseen merchants.',
        'engineering_gate':{'min_passed_per_v6_arm':23,'no_arm_acceptance_regression':True,
            'protected_violations':0,'v6_completed_revision_facts_correct':True,'max_total_cost_ratio':'1.5','max_mean_latency_ratio_each_arm':1.5}})
    print(json.dumps({'registered_cases':24,'runs':144,'free_fixtures':len(checks),'registration_sha256':sha(OUT/'registration.json'),'ledger':ledger()},ensure_ascii=False))


def run():
    if (OUT/'summary.json').exists():raise FileExistsError('Completed results are frozen')
    reg=read(OUT/'registration.json')
    assert all(sha(ROOT/n)==v for n,v in reg['source_sha256'].items())
    assert all(sha(OUT/n)==v for n,v in reg['initial_sha256'].items())
    assert sha(OUT/'cases.json')==reg['cases_sha256'] and sha(OUT/'fixture_checks.json')==reg['fixture_sha256']
    rows={c['id']:c for c in read(OUT/'cases.json')};records=[]
    gate=ProviderGate(ROOT/'evidence/provider_availability.sqlite')
    for ident,condition in reg['jobs']:
        folder=OUT/'runs'/(ident+'-'+condition)
        if (folder/'result.json').exists():records.append(read(folder/'result.json'));continue
        if folder.exists():raise RuntimeError('Partial attempt requires inspection, never automatic duplication')
        client=guarded_business_client(gate);client.ensure_available(MODEL)
        folder.mkdir();case=rows[ident];initial=OUT/'initial'/ident;seed=read(initial/'seed.json')
        store=clone(initial,folder/'operations.sqlite')
        assert digest(store.view(OWNER,seed['draft_id']))==seed['view_digest']
        version,arm=condition.split('_',1);cls=SourceReviewAgent if version=='v5' else ReliabilityAgent
        agent=cls(store,OWNER,seed['draft_id'],client=client,arm=arm,now=instant(case['now']),
            contract=case['contract'],phase='reliability_v1_'+condition)
        save(folder/'attempt.json',{'case_id':ident,'condition':condition,'arm':arm,'version':version,'purpose':agent.purpose,'registration_sha256':sha(OUT/'registration.json')})
        execution=agent.run(case['task']);save(folder/'execution.json',execution)
        measured=evaluate(case,execution,store,seed)
        record={'case_id':ident,'family':case['family'],'condition':condition,'version':version,'arm':arm,'evaluation':measured,
            'has_program_comparison':bool((execution.get('report') or {}).get('revision_comparison')),
            'execution_sha256':sha(folder/'execution.json'),**{k:execution[k] for k in ('run_status','error_type',
                'model_calls','successful_model_calls','tool_calls','successful_tool_calls','input_tokens','output_tokens',
                'accounted_and_reserved_cny','latency_seconds','delegations')}}
        save(folder/'result.json',record);records.append(record)
        print(json.dumps({'completed':len(records),'total':144,'case':ident,'condition':condition,'passed':measured['passed'],
            'failures':measured['failures'],'program_comparison':record['has_program_comparison'],'calls':record['model_calls'],
            'cny':record['accounted_and_reserved_cny']},ensure_ascii=False),flush=True)
    groups={}
    for condition in CONDITIONS:
        chosen=[r for r in records if r['condition']==condition];times=sorted(r['latency_seconds'] for r in chosen)
        cost=sum((Decimal(r['accounted_and_reserved_cny']) for r in chosen),Decimal(0))
        groups[condition]={'runs':len(chosen),'passed':sum(r['evaluation']['passed'] for r in chosen),
            'violations':sum(len(r['evaluation']['constraint_violations']) for r in chosen),
            **{k:sum(r[k] for r in chosen) for k in ('model_calls','successful_model_calls','tool_calls','successful_tool_calls','input_tokens','output_tokens','delegations')},
            'tasks_with_delegation':sum(r['delegations']>0 for r in chosen),'program_comparisons':sum(r['has_program_comparison'] for r in chosen),
            'cost_cny':str(cost),'mean_cost_cny':str(cost/len(chosen)),
            'mean_latency_seconds':statistics.mean(times),'median_latency_seconds':statistics.median(times),
            'p95_latency_seconds':times[(95*len(times)+99)//100-1],
            'failures':dict(Counter(f for r in chosen for f in r['evaluation']['failures'])),
            'families':{f:{'runs':sum(r['family']==f for r in chosen),'passed':sum(r['family']==f and r['evaluation']['passed'] for r in chosen)} for f in FAMILIES}}
    paired={}
    for arm in ARMS:
        counts=Counter()
        for ident in rows:
            pair={r['version']:r['evaluation']['passed'] for r in records if r['case_id']==ident and r['arm']==arm}
            counts['win' if pair['v6']>pair['v5'] else 'loss' if pair['v6']<pair['v5'] else 'tie']+=1
        paired[arm]={k:counts[k] for k in ('win','tie','loss')}
    limits=reg['engineering_gate']
    checks={'minimum_each_arm':all(groups['v6_'+a]['passed']>=limits['min_passed_per_v6_arm'] for a in ARMS),
        'no_arm_acceptance_regression':all(groups['v6_'+a]['passed']>=groups['v5_'+a]['passed'] for a in ARMS),
        'no_protected_violations':all(groups['v6_'+a]['violations']==0 for a in ARMS),
        'total_cost_limit':sum(Decimal(groups['v6_'+a]['cost_cny']) for a in ARMS)<=Decimal('1.5')*sum(Decimal(groups['v5_'+a]['cost_cny']) for a in ARMS),
        'latency_limit_each_arm':all(groups['v6_'+a]['mean_latency_seconds']<=1.5*groups['v5_'+a]['mean_latency_seconds'] for a in ARMS)}
    save(OUT/'summary.json',{'completed_at':datetime.now(timezone.utc).isoformat(),'registration_sha256':sha(OUT/'registration.json'),
        'groups':groups,'paired':paired,'preliminary_gate':checks,'program_comparison_audit':'pending_independent_audit',
        'engineering_gate_passed':None,'ledger_before':reg['ledger_before'],'ledger_after':ledger(),
        'result_sha256':{p.relative_to(OUT).as_posix():sha(p) for p in sorted((OUT/'runs').glob('*/result.json'))},
        'new_real_users':0,'deployment_changed':False,'model_selection_changed':False})
    print(json.dumps({k:v for k,v in read(OUT/'summary.json').items() if k!='result_sha256'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['prepare','run'])
    {'prepare':prepare,'run':run}[p.parse_args().phase]()

"""Prospectively registered recovery after the operator restored the same provider."""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from research import apparel_rationale_audit as base
from research.apparel_rationale_continuation import call_ids
from research.provider_gate import ProviderGate, ProviderHeld

ROOT=base.ROOT
OLD=ROOT/'evidence/apparel_rationale_audit_v2'
PREVIOUS=ROOT/'evidence/apparel_rationale_audit_continuation_v1'
OUT=ROOT/'evidence/apparel_rationale_account_recovery_v1'
MODEL='qwen3.8-max'
LIMIT=130_000_000


def account_denial(error):
    value=str(error).lower()
    return 'http 400' in value and ('overdue-payment' in value or 'arrearage' in value)


class RecoveryClient:
    def __init__(self, allowance, inner=None):
        if allowance not in (1,2):raise ValueError('Invalid remaining transport allowance')
        self.allowance=allowance
        self.inner=inner if inner is not None else base.guarded_business_client()
        self.requests=[]

    def chat(self,messages,**kwargs):
        self.inner.ensure_available(MODEL)
        state=base.ledger()
        if len(self.requests)>=self.allowance:raise RuntimeError('Original two-attempt transport allowance exhausted')
        if state['study_micro_cny']+5_000_000>LIMIT or state['global_micro_cny']+5_000_000>480_000_000:
            raise RuntimeError('Registered study or whole-project budget bound reached')
        before=call_ids()
        record={'attempt_in_recovery':len(self.requests)+1,'started_at':datetime.now(timezone.utc).isoformat()}
        self.requests.append(record)
        kwargs['purpose']=base.PREFIX+kwargs['purpose']
        try:
            return self.inner.chat(messages,**kwargs)
        except Exception as error:
            if account_denial(error):
                ProviderGate(ROOT/'evidence/provider_availability.sqlite').hold('dashscope','payment_required')
            raise
        finally:
            record['budget_call_ids']=sorted(call_ids()-before)
            assert len(record['budget_call_ids'])<=1


def register():
    if OUT.exists():raise FileExistsError('Recovery is already registered')
    receipt=base.read(ROOT/'evidence/apparel_rationale_continuation_check_20260909.json')
    assert receipt['all_checks_passed'] and not receipt['completed_campaign']
    assert len(receipt['pending_unsubmitted'])==24
    prior=base.read(PREVIOUS/'registration.json')
    assert all(base.sha(ROOT/n)==h for n,h in prior['source_sha256'].items())
    assert all(base.sha(ROOT/n)==h for n,h in receipt['source_sha256'].items())
    plans={x['id']:x for x in base.read(PREVIOUS/'schedule.json')}
    schedule=[]
    for item in base.read(OLD/'inputs.json'):
        ident=item['id'];path=PREVIOUS/'rows'/(ident+'.json')
        if ident in receipt['pending_unsubmitted']:
            assert not path.exists() and plans[ident]['previous_submitted_attempts']==0
            schedule.append({'id':ident,'previous_submitted_attempts':0,'remaining_submitted_attempts':2,'reason':'never_submitted'})
        elif path.exists():
            row=base.read(path)
            if account_denial(row.get('error','')):
                assert row['status']=='audit_failed' and 'decision' not in row
                assert len(row['request_accounting'])==1 and plans[ident]['previous_submitted_attempts']==0
                assert not list((PREVIOUS/'raw').glob(ident+'_attempt*.json')) or all(p.name.endswith('.error.json') for p in (PREVIOUS/'raw').glob(ident+'_attempt*.json'))
                schedule.append({'id':ident,'previous_submitted_attempts':1,'remaining_submitted_attempts':1,'reason':'explicit_account_rejection_before_output','previous_row_sha256':base.sha(path)})
    assert len(schedule)==45 and sum(x['reason']=='explicit_account_rejection_before_output' for x in schedule)==21
    sources=prior['source_sha256']|receipt['source_sha256']
    for p in [Path(__file__),ROOT/'research/APPAREL_RATIONALE_ACCOUNT_RECOVERY_PROTOCOL.md',ROOT/'tests/test_apparel_rationale_account_recovery.py',ROOT/'evidence/apparel_rationale_account_recovery_tests_20260909.xml',ROOT/'evidence/apparel_rationale_continuation_check_20260909.json']:
        sources[p.relative_to(ROOT).as_posix()]=base.sha(p)
    base.save(OUT/'schedule.json',schedule)
    base.save(OUT/'registration.json',{'registered_at':datetime.now(timezone.utc).isoformat(),'model':MODEL,'judge_version':base.VERSION,
        'scheduled':45,'newly_submitted':24,'account_recoveries':21,'source_sha256':sources,'schedule_sha256':base.sha(OUT/'schedule.json'),
        'ledger_before':base.ledger(),'combined_study_limit_cny':130,'whole_project_limit_cny':480,
        'user_authorization':'User reported topping up the same Aliyun account and expected service restoration. Project budget remains CNY 480; model-selection subtask remains CNY 100.',
        'budget_amendment':'Previous internal CNY 80 bound was reached because account-error and timeout reservations were retained. CNY 130 is a combined study bound inside the existing whole-project authorization, not additional money authorized by the top-up.',
        'same_inputs_prompt_model_provider':True,'valid_or_schema_invalid_model_outputs_resampled':False,'failed_requests_preserved':True})
    print({'registered':45,'unsubmitted':24,'account_recoveries':21})


def run():
    if (OUT/'summary.json').exists():raise FileExistsError('Completed recovery is immutable')
    reg=base.read(OUT/'registration.json')
    assert all(base.sha(ROOT/n)==h for n,h in reg['source_sha256'].items())
    assert base.sha(OUT/'schedule.json')==reg['schedule_sha256']
    inputs={x['id']:x for x in base.read(OLD/'inputs.json')}
    labels={x['id']:x for x in base.read(OLD/'labels.json')}
    schedule=base.read(OUT/'schedule.json')
    rows=[]
    for plan in schedule:
        ident=plan['id'];path=OUT/'rows'/(ident+'.json');marker=OUT/'attempts'/(ident+'.json')
        if path.exists():rows.append(base.read(path));continue
        if marker.exists():raise RuntimeError('Unfinished request marker; inspect before resuming')
        state=base.ledger()
        if state['study_micro_cny']+5_000_000>LIMIT or state['global_micro_cny']+5_000_000>480_000_000:
            base.save(OUT/'pause.json',{'next_id':ident,'reason':'budget_bound','ledger':state,'recorded':len(rows)})
            print({'paused':True,'reason':'budget_bound','recorded':len(rows)},flush=True);return
        client=RecoveryClient(plan['remaining_submitted_attempts'])
        try:client.inner.ensure_available(MODEL)
        except ProviderHeld:
            base.save(OUT/'pause.json',{'next_id':ident,'reason':'provider_hold','ledger':state,'recorded':len(rows)})
            print({'paused':True,'reason':'provider_hold','recorded':len(rows)},flush=True);return
        base.save(marker,{'started_at':datetime.now(timezone.utc).isoformat(),**plan})
        try:
            measured=base.judge(inputs[ident]['answer'],inputs[ident]['evidence'],client=client,raw_path=OUT/'raw'/(ident+'.json'),model=MODEL)
            row={'status':'audited',**measured}
        except Exception as error:
            row={'status':'audit_failed','error':str(error)[:1000],'transport_failures':getattr(error,'attempts',[])}
        row.update(labels[ident]);row.update(recovery_plan=plan,request_accounting=client.requests)
        assert len(client.requests)+plan['previous_submitted_attempts']<=2
        base.save(path,row);rows.append(row)
        print({'completed':len(rows),'total':45,'id':ident,'status':row['status'],'verdict':row.get('decision',{}).get('verdict')},flush=True)
    combined=[];origins={}
    for ident in inputs:
        paths=[OUT/'rows'/(ident+'.json'),PREVIOUS/'rows'/(ident+'.json'),OLD/'campaign/rows'/(ident+'.json')]
        path=next(p for p in paths if p.exists())
        row=base.read(path)
        assert 'Audit study conservative CNY 20 bound reached' not in row.get('error','')
        combined.append(row);origins[ident]={'path':path.relative_to(ROOT).as_posix(),'sha256':base.sha(path)}
    assert len(combined)==144
    base.save(OUT/'combined_results.json',combined)
    def count(values):
        return {'scheduled':len(values),'audited':sum(r['status']=='audited' for r in values),'audit_failed':sum(r['status']=='audit_failed' for r in values),
                'not_assessable':sum(r['status']=='not_assessable' for r in values),
                'verdicts':{v:sum(r.get('decision',{}).get('verdict')==v for r in values) for v in ('supported','unsupported','insufficient_evidence')},
                'original_task_passed':sum(r['original_strict_task_passed'] for r in values)}
    summary={'completed_at':datetime.now(timezone.utc).isoformat(),'model':MODEL,**count(combined),'recovery':count(rows),
        'groups':{c:count([r for r in combined if r['condition']==c]) for c in sorted({r['condition'] for r in combined})},
        'result_origins':origins,'combined_results_sha256':base.sha(OUT/'combined_results.json'),
        'registration_sha256':base.sha(OUT/'registration.json'),'ledger_after':base.ledger(),'old_account_rejections_preserved':21}
    base.save(OUT/'summary.json',summary)
    print({k:v for k,v in summary.items() if k not in ('result_origins','groups')},flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['register','run']);args=parser.parse_args()
    register() if args.action=='register' else run()

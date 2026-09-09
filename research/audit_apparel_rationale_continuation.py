"""Read-only reconciliation of the paused continuation; never pads unrun cases."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/apparel_rationale_audit_continuation_v1'
OLD=ROOT/'evidence/apparel_rationale_audit_v2'
TARGET=ROOT/'evidence/apparel_rationale_continuation_check_20260909.json'
PREFIX='commerce_apparel_rationale_audit_v1:'

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    if TARGET.exists():raise FileExistsError('Preserve the previous audit receipt')
    reg=read(OUT/'registration.json');schedule=read(OUT/'schedule.json')
    assert sha(OUT/'schedule.json')==reg['schedule_sha256']
    assert all(sha(ROOT/n)==h for n,h in reg['source_sha256'].items())
    assert len(schedule)==109 and len({x['id'] for x in schedule})==109
    inputs={x['id']:x for x in read(OLD/'inputs.json')}
    assert len(inputs)==144
    continued=[];pending=[];ids=set();sources={}
    plans={x['id']:x for x in schedule}
    with closing(sqlite3.connect((ROOT/'evidence/api_budget.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        calls={r['id']:dict(r) for r in db.execute('SELECT * FROM calls WHERE purpose LIKE ?',(PREFIX+'%',))}
        for plan in schedule:
            ident=plan['id'];path=OUT/'rows'/(ident+'.json')
            if not path.exists():
                assert not (OUT/'attempts'/(ident+'.json')).exists(),ident
                pending.append(ident);continue
            row=read(path)
            assert row['id']==ident and row['continuation_plan']==plan
            assert len(row['request_accounting'])+plan['previous_submitted_attempts']<=2
            local_ids=[]
            for request in row['request_accounting']:
                assert len(request['budget_call_ids'])<=1
                for call_id in request['budget_call_ids']:
                    assert call_id not in ids and call_id in calls
                    ids.add(call_id);local_ids.append(call_id)
                    assert calls[call_id]['model']=='qwen3.8-max'
            if row['status']=='audited':
                metadata=row['response_metadata'];call_id=metadata['budget_call_id']
                assert call_id in local_ids and calls[call_id]['status']=='settled'
                assert metadata['requested_model']==metadata['returned_model']=='qwen3.8-max'
                assert int((Decimal(metadata['estimated_cost_cny'])*1_000_000).to_integral_value(rounding='ROUND_CEILING'))==calls[call_id]['charged']
                quotes=[c['quote'] for c in row['decision']['claims']]
                original=inputs[ident]['answer']
                assert ''.join(''.join(quotes).split())==''.join(original.split()),ident
            sources[path.relative_to(ROOT).as_posix()]=sha(path)
            continued.append(row)
        prior=[read(OLD/'campaign/rows'/(ident+'.json')) for ident in inputs if ident not in plans]
        assert len(prior)==35
        covered=prior+continued
        assert len(covered)+len(pending)==144
        assert len(calls)-reg['ledger_before']['study_rows']==len(ids)
        ledger_current=db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    errors=Counter('account_rejection' if 'overdue-payment' in r.get('error','') else 'transport_failure' if r.get('transport_failures') else 'schema_or_audit_failure'
                   for r in continued if r['status']=='audit_failed')
    def stats(rows):
        return {'recorded':len(rows),'audited':sum(r['status']=='audited' for r in rows),
                'audit_failed':sum(r['status']=='audit_failed' for r in rows),
                'not_assessable':sum(r['status']=='not_assessable' for r in rows),
                'verdicts':dict(Counter(r.get('decision',{}).get('verdict') for r in rows if r['status']=='audited'))}
    original_labels=read(OLD/'labels.json')
    if isinstance(original_labels,dict):original_labels=list(original_labels.values())
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'status':'paused_incomplete',
            'registered_original_runs':144,'prior_rows_retained':35,'continuation':stats(continued),'covered':stats(covered),
            'continuation_error_categories':dict(errors),'pending_unsubmitted':pending,
            'groups':{c:stats([r for r in covered if r['condition']==c]) for c in sorted({r['condition'] for r in covered})},
            'new_requests':len(ids),'new_call_statuses':dict(Counter(calls[i]['status'] for i in ids)),
            'new_settled_micro_cny':sum(calls[i]['charged'] or 0 for i in ids),
            'new_uncertain_reserved_micro_cny':sum(calls[i]['reserved'] for i in ids if calls[i]['charged'] is None),
            'study_accounted_micro_cny':sum(r['charged'] if r['charged'] is not None else r['reserved'] for r in calls.values()),
            'global_accounted_micro_cny':ledger_current[0],'global_calls':ledger_current[1],
            'all_checks_passed':True,'completed_campaign':False,'original_frozen_files_changed':0,
            'source_sha256':sources|{p.relative_to(ROOT).as_posix():sha(p) for p in [OUT/'registration.json',OUT/'schedule.json',OUT/'pause.json',Path(__file__)]},
            'limits':['Structural/accounting checks do not establish judge factual correctness.',
                      'Unsubmitted cases are pending, not zero-quality outputs or failed completed audits.',
                      'The original provider hold classified 401/402/403 but missed explicit overdue-payment returned as 400. Consecutive account rejections and their reservations remain recorded; a separate operational hold now prevents further DashScope requests.']}
    TARGET.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','groups','pending_unsubmitted')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

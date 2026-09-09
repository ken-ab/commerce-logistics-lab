"""Separate deterministic evidence checks for the nine flags observed before account recovery."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'evidence/apparel_rationale_audit_v2'
CONT=ROOT/'evidence/apparel_rationale_audit_continuation_v1'
OUT=ROOT/'evidence/apparel_rationale_flag_review_20260909.json'

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def time(value):return datetime.fromisoformat(value.replace('Z','+00:00'))
def segment(proposal,leg):return next(s for s in proposal['route']['segments'] if s['leg_id']==leg)

NOTES={
 'RL-00-0-v5_on_demand':('judge_false_positive','The air service_id is unchanged. A 45-minute actual-departure change does not create a different service instance; the answer explicitly states the delay adjustment.'),
 'RL-00-0-v5_single':('judge_false_positive','Original deadline is a constraint, not the old estimated arrival. Shipping constraints and selected garments are unchanged.'),
 'RL-03-0-v5_on_demand':('judge_false_positive','The old stored proposal has no observed events; a new revision incorporating cancellation evidence does not prove the old proposal had incorporated it.'),
 'RL-03-1-v6_on_demand':('judge_false_positive','Both road legs retain their service identifiers, departure and arrival times. The downstream waiting interval shrinks because the incoming flight changes; this does not change the road service instance.'),
 'RL-08-1-v5_coordinator':('contextually_supported_ambiguous_wording','The task reviews the current version 2 proposal, whose flight is the stated May 10 04:00 service. The judge instead compares version 1. Use “pre-revision version 2” to avoid the ambiguous word original.'),
 'RL-08-1-v5_single':('contextually_supported_ambiguous_wording','The reference for this second revision is the current version 2 proposal. Its May 10 04:00 service matches the answer. “Original” can ambiguously suggest version 1.'),
 'RL-01-0-v5_coordinator':('confirmed_answer_error_judge_reason_incorrect','The replacement departs 720 minutes after the old recorded departure, not 1620 minutes. 1620 is the delayed old service duration. The judge flagged a different, defensible old-event statement and incorrectly accepted the 1620-minute replacement shift.'),
 'RL-01-1-v6_on_demand':('judge_false_positive','The old stored route lacks the published event. revision_comparison.old_service_effect is a new assessment of that service, not evidence that the stored old route already included the event.'),
 'RL-09-1-v6_on_demand':('judge_false_positive','The old stored route omits the active delay, and its recorded times fail the event-aware validity check. Explaining invalidity by the published delay is consistent with that record.')}

def main():
    if OUT.exists():raise FileExistsError('Preserve the existing dated review')
    inputs={r['id']:r for r in read(BASE/'inputs.json')}
    reviews=[]
    for ident,(classification,note) in NOTES.items():
        path=CONT/'rows'/(ident+'.json')
        if not path.exists():path=BASE/'campaign/rows'/(ident+'.json')
        row=read(path);assert row['decision']['verdict']=='unsupported'
        e=inputs[ident]['evidence'];before=e['HOST_BEFORE'];after=e['HOST_AFTER']
        old=max(before['proposals'],key=lambda p:p['version']);new=max(after['proposals'],key=lambda p:p['version'])
        air_old=segment(old,'HK-EU-AIR');air_new=segment(new,'HK-EU-AIR')
        proof={'old_version':old['version'],'new_version':new['version'],
          'old_air':air_old,'new_air':air_new,
          'replacement_departure_difference_minutes':(time(air_new['departure_at'])-time(air_old['departure_at'])).total_seconds()/60,
          'shipping_constraints_unchanged':old['route']['shipping_constraints']==new['route']['shipping_constraints'],
          'selections_unchanged':before['selections']==after['selections'],
          'old_observed_event_ids':old['route']['observed_event_ids'],
          'new_observed_event_ids':new['route']['observed_event_ids']}
        assert proof['shipping_constraints_unchanged'] and proof['selections_unchanged']
        if ident=='RL-01-0-v5_coordinator':assert proof['replacement_departure_difference_minutes']==720
        if ident=='RL-00-0-v5_on_demand':
            assert air_old['service_id']==air_new['service_id'] and proof['replacement_departure_difference_minutes']==45
        if ident=='RL-03-1-v6_on_demand':
            roads=[]
            for leg in ('SZ-HK-ROAD','EU-DE-ROAD'):
                a,b=segment(old,leg),segment(new,leg)
                assert all(a[k]==b[k] for k in ('service_id','departure_at','arrival_at'))
                roads.append({'leg_id':leg,'old':a,'new':b})
            proof['retained_roads']=roads
        if ident.startswith('RL-08-1'):
            assert old['version']==2 and new['version']==3
            assert air_old['nominal_departure']=='2028-05-10T04:00:00Z'
            proof['review_task']=e['TASK']['user_request']
        if ident in ('RL-03-0-v5_on_demand','RL-01-1-v6_on_demand','RL-09-1-v6_on_demand'):
            assert not proof['old_observed_event_ids'] and proof['new_observed_event_ids']
        reviews.append({'id':ident,'classification':classification,'review_note':note,
                        'source_judge_row':path.relative_to(ROOT).as_posix(),'source_judge_sha256':sha(path),
                        'original_flagged_claims':[c for c in row['decision']['claims'] if c['status']!='supported'],
                        'deterministic_checks':proof})
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'scope':'All nine unsupported judgments available before same-provider account recovery; targeted semantic review, not a full review of every supported answer.',
            'input_sha256':sha(BASE/'inputs.json'),'source_sha256':sha(Path(__file__)),
            'reviewer':'Codex with explicit program checks over the original evidence; not independent human annotation',
            'original_judge_rows_modified':False,'original_business_scores_modified':False,'records':reviews}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print({'reviewed':len(reviews),'judge_false_positive':6,'ambiguous_reference':2,'confirmed_numeric_error':1})

if __name__=='__main__':main()

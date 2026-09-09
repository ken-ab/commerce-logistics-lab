"""Targeted evidence review of recovery flags; original model verdicts stay unchanged."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'evidence/apparel_rationale_audit_v2'
RECOVERY=ROOT/'evidence/apparel_rationale_account_recovery_v1'
OUT=ROOT/'evidence/apparel_rationale_recovery_flag_review_20260909.json'
NOTES={
 'RL-00-1-v5_on_demand':'Keeping the requested deadline is a constraint, not a claim that the final arrival changed. The air departure changes by 90 minutes, with a shorter downstream wait and the same final arrival before the unchanged deadline.',
 'RL-09-0-v5_single':'The 27-hour delay affects the old service. The revision chooses a different, unaffected service 12 hours after the old nominal departure; the reduced transfer wait permits the same final arrival before the original deadline.',
 'RL-09-0-v5_coordinator':'The old stored route has no event evidence and its departure omits the delay. The reported validation errors departure_does_not_include_events and segment_event_evidence_incomplete support the statement that the old proposal had not incorporated the delay.',
 'RL-09-0-v6_coordinator':'A reference to the affected service is not incorporation of its delay into the stored schedule. The old route omits event IDs and actual delayed times; the invalidity explanation agrees with the validity tool.',
 'RL-10-1-v6_single':'The judge assigns contradicted while its own explanation concludes Supported. Direct arithmetic confirms the new cost is within the unchanged budget and arrival precedes the unchanged deadline.'}

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def time(s):return datetime.fromisoformat(s.replace('Z','+00:00'))

def main():
 if OUT.exists():raise FileExistsError('Preserve the dated review')
 inputs={r['id']:r for r in read(BASE/'inputs.json')}
 rows=read(RECOVERY/'combined_results.json')
 new_flags={r['id'] for r in rows if r.get('decision',{}).get('verdict')=='unsupported' and 'recovery_plan' in r}
 assert new_flags==set(NOTES)
 records=[]
 for ident,note in NOTES.items():
  path=RECOVERY/'rows'/(ident+'.json');row=read(path);e=inputs[ident]['evidence']
  before,after=e['HOST_BEFORE'],e['HOST_AFTER']
  old=max(before['proposals'],key=lambda p:p['version']);new=max(after['proposals'],key=lambda p:p['version'])
  constraints=new['route']['shipping_constraints']
  a=next(s for s in old['route']['segments'] if s['leg_id']=='HK-EU-AIR')
  b=next(s for s in new['route']['segments'] if s['leg_id']=='HK-EU-AIR')
  assert old['route']['shipping_constraints']==constraints and before['selections']==after['selections']
  assert new['route']['total_cost_cents']<=constraints['budget_cents']
  assert time(new['route']['arrival_at'])<=time(constraints['deadline_at'])
  assert not old['route']['observed_event_ids'] and new['route']['observed_event_ids']
  departure_difference=(time(b['departure_at'])-time(a['departure_at'])).total_seconds()/60
  assert departure_difference==(90 if ident.startswith('RL-00') else 720)
  records.append({'id':ident,'classification':'judge_false_positive','review_note':note,
    'source_judge_sha256':sha(path),'source_judge_row':path.relative_to(ROOT).as_posix(),
    'original_flagged_claims':[c for c in row['decision']['claims'] if c['status']!='supported'],
    'deterministic_checks':{'shipping_constraints_unchanged':True,'selections_unchanged':True,
      'cost_cents':new['route']['total_cost_cents'],'constraints':constraints,'arrival_at':new['route']['arrival_at'],
      'old_air':a,'new_air':b,'departure_difference_minutes':departure_difference,
      'old_observed_event_ids':old['route']['observed_event_ids'],'new_observed_event_ids':new['route']['observed_event_ids']}})
 result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'scope':'All five unsupported judgments from the account recovery; no review of every supported, missing or schema-invalid answer.',
   'reviewer':'Codex with program checks over original evidence; not independent human annotation',
   'input_sha256':sha(BASE/'inputs.json'),'source_sha256':sha(Path(__file__)),
   'original_judge_rows_modified':False,'original_business_scores_modified':False,'records':records}
 OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print({'reviewed':len(records),'judge_false_positive':len(records),'scores_changed':False})

if __name__=='__main__':main()

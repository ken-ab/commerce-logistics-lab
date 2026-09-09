"""Resume only budget-interrupted audits, without resampling any completed response."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from research import apparel_rationale_audit as base

ROOT = base.ROOT
OLD = ROOT / 'evidence/apparel_rationale_audit_v2'
OUT = ROOT / 'evidence/apparel_rationale_audit_continuation_v1'
MODEL = 'qwen3.8-max'
LIMIT = 80_000_000
INTERRUPTED = 'Audit study conservative CNY 20 bound reached'


def call_ids():
    with closing(sqlite3.connect((ROOT/'evidence/api_budget.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
        return {r[0] for r in db.execute('SELECT id FROM calls WHERE substr(purpose,1,?)=?', (len(base.PREFIX), base.PREFIX))}


def previous_attempts(raw_folder, ident):
    files = sorted(raw_folder.glob(ident + '*'))
    if not files:
        return 0
    assert len(files) == 1 and files[0].name == ident + '_attempt1.error.json', ident
    error = base.read(files[0])
    assert error['attempt'] == 1 and error['retryable'] is True, ident
    assert 'message' not in error and 'usage' not in error
    return 1


class ContinuationClient:
    def __init__(self, allowance, inner=None):
        assert allowance in (1, 2)
        self.allowance = allowance
        self.inner = inner if inner is not None else base.guarded_business_client()
        self.requests = []

    def chat(self, messages, **kwargs):
        if len(self.requests) >= self.allowance:
            raise RuntimeError('Registered total transport-attempt allowance exhausted')
        if base.ledger()['study_micro_cny'] + 5_000_000 > LIMIT:
            raise RuntimeError('Combined rationale audit CNY 80 bound reached')
        before = call_ids()
        record = {'attempt_in_continuation': len(self.requests) + 1, 'started_at': datetime.now(timezone.utc).isoformat()}
        self.requests.append(record)
        kwargs['purpose'] = base.PREFIX + kwargs['purpose']
        try:
            return self.inner.chat(messages, **kwargs)
        finally:
            record['budget_call_ids'] = sorted(call_ids() - before)
            assert len(record['budget_call_ids']) <= 1, 'Unexpected concurrent writer to the same research prefix'


def register():
    if OUT.exists():
        raise FileExistsError('Continuation already registered')
    reg = base.read(OLD/'registration.json')
    summary = base.read(OLD/'campaign/summary.json')
    audit_path = ROOT/'evidence/apparel_rationale_audit_checks_20260909.json'
    audit = base.read(audit_path)
    assert audit['all_checks_passed'] and audit['study_calls'] == 82
    assert base.sha(OLD/'registration.json') == summary['registration_sha256']
    assert base.read(OLD/'calibration/summary.json')['gate_passed']
    assert base.sha(OLD/'inputs.json') == reg['input_sha256']
    assert base.sha(OLD/'labels.json') == reg['label_sha256']
    for f, h in reg['source_sha256'].items():
        assert base.sha(ROOT/f) == h
    items = base.read(OLD/'inputs.json')
    schedule = []
    for item in items:
        ident = item['id']
        row = base.read(OLD/'campaign/rows'/(ident+'.json'))
        if row.get('error') == INTERRUPTED:
            assert row['status'] == 'audit_failed' and 'decision' not in row and item['answer']
            prior = previous_attempts(OLD/'campaign/raw', ident)
            schedule.append({'id':ident, 'previous_submitted_attempts':prior, 'remaining_submitted_attempts':2-prior})
    assert len(schedule) == 109 and sum(x['previous_submitted_attempts'] for x in schedule) == 1
    sources = {**reg['source_sha256'], **audit['verified_sha256']}
    for path in [Path(__file__).resolve(), ROOT/'research/APPAREL_RATIONALE_CONTINUATION_PROTOCOL.md',
                 audit_path, ROOT/'research/audit_apparel_rationale_audit.py',
                 ROOT/'tests/test_apparel_rationale_continuation.py', ROOT/'evidence/apparel_rationale_continuation_tests_20260909.xml']:
        sources[path.relative_to(ROOT).as_posix()] = base.sha(path)
    assert all(base.sha(ROOT/f) == h for f,h in sources.items())
    base.save(OUT/'schedule.json', schedule)
    base.save(OUT/'registration.json', {
        'registered_at':datetime.now(timezone.utc).isoformat(), 'model':MODEL, 'judge_version':base.VERSION,
        'original_attempts':144, 'scheduled_continuations':109, 'unsubmitted':108, 'one_transport_attempt_already_used':1,
        'previous_summary':'evidence/apparel_rationale_audit_v2/campaign/summary.json',
        'calibration_inherited_from':'evidence/apparel_rationale_audit_v2/calibration/summary.json',
        'source_sha256':sources, 'schedule_sha256':base.sha(OUT/'schedule.json'),
        'ledger_before':base.ledger(), 'same_budget_prefix':base.PREFIX, 'combined_study_limit_cny':80,
        'project_limit_cny':480, 'normal_expected_total_cny':[35,50],
        'completed_responses_never_resampled':True, 'original_inputs_and_protocol_unchanged':True})
    print(json.dumps({'registered':True,'continuations':109,'previous_attempts':1,'ledger':base.ledger()},ensure_ascii=False))


def run():
    if (OUT/'summary.json').exists():
        raise FileExistsError('Completed continuation is frozen')
    reg = base.read(OUT/'registration.json')
    assert all(base.sha(ROOT/f) == h for f,h in reg['source_sha256'].items())
    assert base.sha(OUT/'schedule.json') == reg['schedule_sha256']
    schedule = base.read(OUT/'schedule.json')
    items = {i['id']:i for i in base.read(OLD/'inputs.json')}
    labels = {i['id']:i for i in base.read(OLD/'labels.json')}
    rows = []
    for plan in schedule:
        ident = plan['id']
        path = OUT/'rows'/(ident+'.json')
        marker = OUT/'attempts'/(ident+'.json')
        if path.exists():
            rows.append(base.read(path)); continue
        if marker.exists():
            raise RuntimeError('Unfinished request marker needs inspection; do not duplicate it')
        if base.ledger()['study_micro_cny'] + 5_000_000 > LIMIT:
            base.save(OUT/'pause.json', {'next_id':ident, 'reason':'combined_study_bound', 'ledger':base.ledger(), 'completed':len(rows)})
            print(json.dumps({'paused':True,'completed':len(rows),'total':len(schedule)},ensure_ascii=False),flush=True)
            return
        client = ContinuationClient(plan['remaining_submitted_attempts'])
        client.inner.ensure_available(MODEL)
        base.save(marker, {'started_at':datetime.now(timezone.utc).isoformat(), **plan})
        raw = OUT/'raw'/(ident+'.json'); raw.parent.mkdir(parents=True, exist_ok=True)
        try:
            measured = base.judge(items[ident]['answer'], items[ident]['evidence'], client=client, raw_path=raw, model=MODEL)
            row = {'status':'audited', **measured}
        except Exception as error:
            row = {'status':'audit_failed', 'error':str(error)[:1000], 'transport_failures':getattr(error,'attempts',[])}
        row.update(labels[ident])
        row.update(continuation_plan=plan, request_accounting=client.requests)
        assert len(client.requests) + plan['previous_submitted_attempts'] <= 2
        base.save(path, row); rows.append(row)
        print(json.dumps({'completed':len(rows),'total':109,'id':ident,'status':row['status'],
                          'verdict':row.get('decision',{}).get('verdict')},ensure_ascii=False),flush=True)
    continued = {r['id']:r for r in rows}
    combined, origins = [], {}
    for ident in items:
        path = OUT/'rows'/(ident+'.json') if ident in continued else OLD/'campaign/rows'/(ident+'.json')
        combined.append(base.read(path))
        origins[ident] = {'path':path.relative_to(ROOT).as_posix(), 'sha256':base.sha(path)}
    assert len(combined) == 144
    def summarize(values):
        return {'scheduled':len(values), 'audited':sum(r['status']=='audited' for r in values),
            'audit_failed':sum(r['status']=='audit_failed' for r in values),
            'not_assessable':sum(r['status']=='not_assessable' for r in values),
            'original_task_passed':sum(r['original_strict_task_passed'] for r in values),
            'original_task_and_judge_supported':sum(r['original_strict_task_passed'] and r.get('decision',{}).get('verdict')=='supported' for r in values),
            'verdicts':{v:sum(r.get('decision',{}).get('verdict')==v for r in values) for v in ['supported','unsupported','insufficient_evidence']}}
    base.save(OUT/'combined_results.json',combined)
    summary = {'completed_at':datetime.now(timezone.utc).isoformat(), 'model':MODEL, **summarize(combined),
        'groups':{c:summarize([r for r in combined if r['condition']==c]) for c in sorted({r['condition'] for r in combined})},
        'continued':summarize(rows), 'original_v2_rows_retained':35, 'original_v2_files_changed':0,
        'result_origins':origins, 'combined_results_sha256':base.sha(OUT/'combined_results.json'),
        'ledger_after':base.ledger(), 'registration_sha256':base.sha(OUT/'registration.json')}
    base.save(OUT/'summary.json', summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='result_origins'},ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['register','run']); args=parser.parse_args()
    register() if args.mode=='register' else run()

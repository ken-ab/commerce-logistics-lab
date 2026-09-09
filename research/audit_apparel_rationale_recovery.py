"""Reconcile the completed recovery without changing or repeating any judgment."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'evidence/apparel_rationale_account_recovery_v1'
OLD = ROOT / 'evidence/apparel_rationale_audit_v2'
TARGET = ROOT / 'evidence/apparel_rationale_recovery_check_20260909.json'
PREFIX = 'commerce_apparel_rationale_audit_v1:'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def statistics(rows):
    return {'scheduled': len(rows),
            'audited': sum(r['status'] == 'audited' for r in rows),
            'audit_failed': sum(r['status'] == 'audit_failed' for r in rows),
            'not_assessable': sum(r['status'] == 'not_assessable' for r in rows),
            'verdicts': {v: sum(r.get('decision', {}).get('verdict') == v for r in rows)
                         for v in ('supported', 'unsupported', 'insufficient_evidence')},
            'original_task_passed': sum(r['original_strict_task_passed'] for r in rows)}


def failure_type(row):
    error = row.get('error', '')
    if '[Errno 2]' in error and 'account_recovery_v1' in error:
        return 'local_raw_save_failure_paid_response_lost'
    if 'overdue-payment' in error:
        return 'account_rejection'
    if row.get('transport_failures') or 'transport allowance exhausted' in error:
        return 'transport_failure'
    return 'schema_or_audit_failure'


def main():
    if TARGET.exists():
        raise FileExistsError('Preserve the completed reconciliation')
    reg = read(OUT / 'registration.json')
    summary = read(OUT / 'summary.json')
    plans = read(OUT / 'schedule.json')
    incident = read(OUT / 'output_directory_incident.json')
    assert len(plans) == 45 and len({r['id'] for r in plans}) == 45
    assert Counter(r['reason'] for r in plans) == {
        'never_submitted': 24, 'explicit_account_rejection_before_output': 21}
    assert sha(OUT / 'schedule.json') == reg['schedule_sha256']
    assert sha(OUT / 'registration.json') == summary['registration_sha256']
    assert all(sha(ROOT / name) == value for name, value in reg['source_sha256'].items())
    inputs = {r['id']: r for r in read(OLD / 'inputs.json')}
    rows = read(OUT / 'combined_results.json')
    assert sha(OUT / 'combined_results.json') == summary['combined_results_sha256']
    assert len(rows) == len(inputs) == 144 and {r['id'] for r in rows} == set(inputs)
    assert all(summary[key] == value for key, value in statistics(rows).items())
    for condition, expected in summary['groups'].items():
        actual = statistics([r for r in rows if r['condition'] == condition])
        assert actual == expected and actual['scheduled'] == 24
    for row in rows:
        origin = summary['result_origins'][row['id']]
        assert sha(ROOT / origin['path']) == origin['sha256']
        assert read(ROOT / origin['path']) == row
        if row['status'] == 'audited':
            quotes = ''.join(c['quote'] for c in row['decision']['claims'])
            assert ''.join(quotes.split()) == ''.join(inputs[row['id']]['answer'].split())
            assert all(set(c['evidence_ids']) <= set(inputs[row['id']]['evidence'])
                       for c in row['decision']['claims'])
    recovered, ids, lost, raw_ids = [], set(), set(), set()
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        calls = {r['id']: dict(r) for r in db.execute('SELECT * FROM calls WHERE purpose LIKE ?', (PREFIX + '%',))}
        for plan in plans:
            ident = plan['id']
            row = read(OUT / 'rows' / (ident + '.json'))
            assert row['id'] == ident and row['recovery_plan'] == plan
            assert len(row['request_accounting']) + plan['previous_submitted_attempts'] <= 2
            local = set()
            for request in row['request_accounting']:
                assert len(request['budget_call_ids']) <= 1
                for call_id in request['budget_call_ids']:
                    assert call_id not in ids and calls[call_id]['model'] == 'qwen3.8-max'
                    assert request['started_at'] >= reg['registered_at']
                    local.add(call_id)
                    ids.add(call_id)
            raw_path = OUT / 'raw' / (ident + '.json')
            if raw_path.exists():
                raw = read(raw_path)
                call_id = raw['budget_call_id']
                assert call_id in local and calls[call_id]['status'] == 'settled'
                assert raw['requested_model'] == raw['returned_model'] == 'qwen3.8-max'
                charged = int((Decimal(raw['estimated_cost_cny']) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
                assert charged == calls[call_id]['charged']
                raw_ids.add(call_id)
                if row['status'] == 'audited':
                    assert all(raw[k] == v for k, v in row['response_metadata'].items())
            elif row['status'] == 'audited':
                raise AssertionError('Successful judgment has no raw response')
            if failure_type(row) == 'local_raw_save_failure_paid_response_lost':
                assert row['status'] == 'audit_failed' and not raw_path.exists()
                assert len(local) == 1 and calls[next(iter(local))]['status'] == 'settled'
                lost.add(ident)
            recovered.append(row)
        assert lost == set(incident['affected_ids']) and len(lost) == 6
        assert len(calls) - reg['ledger_before']['study_rows'] == len(ids)
        assert len(ids) == len(raw_ids) + len(lost) + sum(calls[i]['status'] != 'settled' for i in ids)
        global_amount, global_count = db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    study_amount = sum(c['charged'] if c['charged'] is not None else c['reserved'] for c in calls.values())
    assert global_amount <= 480_000_000 and study_amount <= 130_000_000
    assert summary['ledger_after'] == {'global_micro_cny': global_amount, 'global_rows': global_count,
                                       'study_micro_cny': study_amount, 'study_rows': len(calls)}
    assert statistics(recovered) == summary['recovery']
    ledger_path = OUT / 'study_accounting_export.json'
    if ledger_path.exists():
        raise FileExistsError('Preserve the accounting export')
    export = {'scope': 'Only this claim-audit study; no API secrets or unrelated project ledger.',
              'recorded_at': datetime.now(timezone.utc).isoformat(), 'records': list(calls.values())}
    ledger_path.write_text(json.dumps(export, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'all_checks_passed': True,
              'completed_registered_schedule': True, 'valid_judgment_coverage_complete': False,
              'combined': statistics(rows), 'recovery': statistics(recovered),
              'groups': summary['groups'],
              'failure_categories': dict(Counter(failure_type(r) for r in rows if r['status'] == 'audit_failed')),
              'recovery_failure_categories': dict(Counter(failure_type(r) for r in recovered if r['status'] == 'audit_failed')),
              'new_requests': len(ids), 'new_call_statuses': dict(Counter(calls[i]['status'] for i in ids)),
              'new_settled_micro_cny': sum(calls[i]['charged'] or 0 for i in ids),
              'new_uncertain_reserved_micro_cny': sum(calls[i]['reserved'] for i in ids if calls[i]['charged'] is None),
              'study_settled_micro_cny': sum(r['charged'] or 0 for r in calls.values()),
              'study_uncertain_reserved_micro_cny': sum(r['reserved'] for r in calls.values() if r['charged'] is None),
              'study_accounted_micro_cny': study_amount, 'global_accounted_micro_cny': global_amount,
              'global_calls': global_count, 'lost_paid_raw_responses': sorted(lost),
              'source_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in
                                [OUT/'registration.json', OUT/'schedule.json', OUT/'summary.json',
                                 OUT/'combined_results.json', OUT/'output_directory_incident.json', ledger_path, Path(__file__)]},
              'limits': ['Structural and accounting checks do not validate the judge semantics.',
                         'Invalid, missing and locally lost outputs remain unscored; they are not assumed correct.',
                         'Earlier account rejections and uncertain reservations remain in the study accounting.',
                         'No successful or schema-invalid model response was resampled for a better verdict.']}
    TARGET.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('source_sha256', 'groups', 'lost_paid_raw_responses')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

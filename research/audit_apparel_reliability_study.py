"""Offline audit of first-attempt v5/v6 trials; no agent or planner imports.

Business arithmetic reuses the earlier separately implemented audit, not the
runtime evaluator. New service and explanation checks start from raw SQLite.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
import statistics

from research.audit_apparel_candidate_validation import at, connect, digest, micro, pointer, read, route_check, sha
from research.audit_apparel_expansion_study import order_projection, proposal_valid, recalculate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_reliability_study_v1'
OUT = ROOT / 'evidence/apparel_reliability_audit_20260909.json'
ARMS = ('single', 'coordinator', 'on_demand')


def utc(value):
    stamp = at(value) if isinstance(value, str) else value
    assert stamp.tzinfo is not None
    return stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def cancelled_departure_cut(case, corridor, events, route):
    """A complete cut for these fixtures, without calling a route search."""
    assert case['family'] == 'exhausted_departures'
    assert route['status'] == 'infeasible' and not route.get('segments', [])
    assert route['adjustment_options'] and all(o['requires_user_choice'] for o in route['adjustment_options'])
    start, end = map(at, (case['request']['shipping']['ready_at'], case['request']['shipping']['deadline_at']))
    active = [e for e in events if at(e['published_at']) <= at(case['now'])]
    # No prior service is delayed into the window in these cancellation fixtures.
    assert active and all(e['kind'] == 'cancel' and e['delay_minutes'] == 0 for e in active)
    crossing = [leg for leg in corridor['legs'] if leg['destination'] == 'EU-HUB']
    assert {leg['mode'] for leg in crossing} == {'air', 'rail', 'sea'}
    # Removing these three edges really disconnects source from destination.
    reachable = {corridor['origin']}
    for _ in corridor['legs']:
        for leg in corridor['legs']:
            if leg not in crossing and leg['origin'] in reachable:
                reachable.add(leg['destination'])
    assert corridor['destination'] not in reachable
    checked = []
    for leg in crossing:
        if leg['mode'] != 'air':
            assert timedelta(minutes=leg['duration_minutes']) > end - start
            continue
        first = at(corridor['anchor_at']) + timedelta(minutes=leg['offset_minutes'])
        period = timedelta(minutes=leg['period_minutes'])
        n = math.ceil((start - first) / period)
        nominal = first + n * period
        while nominal <= end:
            matching = [e for e in active if e['leg_id'] == leg['id'] and at(e['nominal_departure']) == nominal]
            assert matching and any(e['kind'] == 'cancel' for e in matching)
            checked.append(leg['id'] + '@' + utc(nominal))
            nominal += period
    assert checked
    return checked


def check_comparison(value, proposals, events, now):
    """Check every reported field using immutable proposal payloads and events."""
    by_id = {p['proposal_id']: p for p in proposals}
    old, new = by_id[value['old_proposal_id']], by_id[value['new_proposal_id']]
    retained = old['proposal_id'] == new['proposal_id']
    assert old['draft_id'] == new['draft_id'] and old['request_revision'] == new['request_revision']
    assert retained or (new['previous_proposal_id'] == old['proposal_id'] and new['version'] == old['version'] + 1)
    expected_header = {'schema_version': 'proposal-comparison-v1', 'as_of': utc(now),
        'old_proposal_id': old['proposal_id'], 'new_proposal_id': new['proposal_id'],
        'old_version': old['version'], 'new_version': new['version'], 'retained_proposal': retained,
        'new_route_status': new['route']['status'], 'new_cost_cents': new['route'].get('total_cost_cents'),
        'new_arrival_at': new['route'].get('arrival_at')}
    assert all(value.get(k) == v for k, v in expected_header.items()), 'comparison header differs from state'
    event_map = {}
    for e in events:
        if at(e['published_at']) <= at(now):
            assert e['event_id'] not in event_map or event_map[e['event_id']] == e
            assert e['kind'] in ('cancel', 'delay') and type(e['delay_minutes']) is int and e['delay_minutes'] >= 0
            assert e['kind'] != 'cancel' or e['delay_minutes'] == 0
            event_map[e['event_id']] = e
    sides = [{s['leg_id']: s for s in p['route'].get('segments', [])} for p in (old, new)]
    assert all(len(side) == len(p['route'].get('segments', [])) for side, p in zip(sides, (old, new)))
    wanted_legs = list(sides[0]) + [leg for leg in sides[1] if leg not in sides[0]]
    assert [item['leg_id'] for item in value['segments']] == wanted_legs
    for item in value['segments']:
        a, b = (side.get(item['leg_id']) for side in sides)
        assert item['old'] == a and item['new'] == b
        change = 'added' if not a else 'removed' if not b else 'retained_service' if a['service_id'] == b['service_id'] else 'replaced_service'
        assert item['change'] == change
        assert item['wait_change_minutes'] == (b['wait_minutes'] - a['wait_minutes'] if a and b else None)
        for label, segment in (('old', a), ('new', b)):
            effect = item[label + '_service_effect']
            if segment is None:
                assert effect is None
                continue
            assert segment['service_id'] == item['leg_id'] + '@' + utc(segment['nominal_departure'])
            matches = [e for e in event_map.values() if e['leg_id'] == item['leg_id'] and at(e['nominal_departure']) == at(segment['nominal_departure'])]
            cancelled = any(e['kind'] == 'cancel' for e in matches)
            delay = sum(e['delay_minutes'] for e in matches if e['kind'] == 'delay')
            departure = None if cancelled else utc(at(segment['nominal_departure']) + timedelta(minutes=delay))
            expected = {'event_ids': sorted(e['event_id'] for e in matches), 'cancelled': cancelled,
                'delay_minutes': delay, 'departure_after_known_events': departure}
            assert effect == expected, (item['leg_id'], label, 'incorrect event attribution')
            if label == 'new' and not retained and new['route']['status'] == 'planned':
                assert not cancelled and at(segment['departure_at']) == at(departure)
                assert set(segment['event_ids']) == set(expected['event_ids'])
    return old, new


def check_rendered_text(text, value, report):
    """Verify the narrow program template, not the model's free-text rationale."""
    lines = text.splitlines()
    assert lines[0] == '程序核对的运输变化（时间均为 UTC；运输数据为模拟）：'
    assert lines[1] == f"提案版本 {value['old_version']} → {value['new_version']}，当前路线状态：{value['new_route_status']}。"
    cursor = 2
    for item in value['segments']:
        a, b, event = item['old'], item['new'], item['old_service_effect']
        if a and (event['cancelled'] or event['delay_minutes']):
            line = lines[cursor]; cursor += 1
            assert line.startswith(item['leg_id'] + '：原班次 ' + a['nominal_departure'])
            if event['cancelled']:
                assert '已取消' in line and ', '.join(event['event_ids']) in line and '应于' not in line
            else:
                assert f"累计延误 {event['delay_minutes']} 分钟" in line
                assert '该原班次按当前事件应于 ' + event['departure_after_known_events'] + ' 出发' in line
        line = lines[cursor]; cursor += 1
        if item['change'] == 'replaced_service':
            assert line.startswith('改选另一班 ' + b['service_id'])
            assert '实际出发 ' + b['departure_at'] in line
            assert '该新班次事件：' + (', '.join(item['new_service_effect']['event_ids']) or '无') in line
        elif item['change'] == 'retained_service':
            assert line.startswith('班次身份未变：' + b['service_id'])
            assert '提案记录的出发时间 ' + b['departure_at'] in line
            assert f"等待由 {a['wait_minutes']:g} 变为 {b['wait_minutes']:g} 分钟" in line
        elif item['change'] == 'removed':
            assert line == f"新版不再包含 {a['service_id']}；当前路线状态为 {value['new_route_status']}。"
        else:
            assert line == f"新增班次 {b['service_id']}，实际出发 {b['departure_at']}。"
    assert cursor == len(lines)
    assert report['answer'].endswith('\n\n' + text)
    assert 'not validated' in report['rationale_notice']


def program_audit(execution, case, events, version):
    report = execution.get('report') or {}
    expected = version == 'v6' and execution['run_status'] == 'completed' and case['contract']['mode'] == 'review_proposal'
    comparison = report.get('revision_comparison')
    failures, tool_checked = [], 0
    proposals = execution['after']['proposals']
    for trace in execution['traces']:
        if trace['kind'] == 'tool' and trace['success'] and trace['result'].get('revision_comparison'):
            try:
                check_comparison(trace['result']['revision_comparison'], proposals, events, case['now'])
                tool_checked += 1
            except (AssertionError, KeyError, ValueError, TypeError, IndexError) as error:
                failures.append({'where': 'tool:' + trace['observation_id'], 'error': str(error)})
    if expected:
        try:
            assert comparison, 'completed proposal review has no program comparison'
            old, new = check_comparison(comparison, proposals, events, case['now'])
            assert new['proposal_id'] == proposals[-1]['proposal_id'] == report['decision']['proposal_id']
            assert old['proposal_id'] == (new['previous_proposal_id'] or new['proposal_id'])
            check_rendered_text(report['revision_explanation'], comparison, report)
            receipts = [t for t in execution['traces'] if t['kind'] == 'revision_comparison']
            assert receipts and receipts[-1]['comparison'] == comparison and receipts[-1]['free_text_rationale_validated'] is False
        except (AssertionError, KeyError, ValueError, TypeError, IndexError) as error:
            failures.append({'where': 'final', 'error': str(error)})
    elif comparison:
        failures.append({'where': 'final', 'error': 'comparison outside registered completion scope'})
    return {'expected_completed_review': expected, 'present': bool(comparison), 'tool_comparisons_checked': tool_checked,
        'passed': not failures, 'failures': failures, 'free_text_rationale_checked': False}


def audit_run(case, condition, reg_hash, corridor, budget):
    version, arm = condition.split('_', 1)
    folder = DATA / 'runs' / (case['id'] + '-' + condition)
    result, execution, attempt = (read(folder / name) for name in ('result.json', 'execution.json', 'attempt.json'))
    assert attempt['registration_sha256'] == reg_hash and attempt['case_id'] == case['id'] and attempt['arm'] == arm
    assert attempt['condition'] == condition and attempt['version'] == version
    assert attempt['purpose'].startswith('commerce_apparel:reliability_v1_' + condition + ':')
    assert result['condition'] == condition and result['version'] == version
    assert sha(folder / 'execution.json') == result['execution_sha256']
    seed = read(DATA / 'initial' / case['id'] / 'seed.json')
    before, after = execution['before'], execution['after']
    assert digest(before) == seed['view_digest']
    with closing(connect(folder / 'operations.sqlite')) as db:
        row = db.execute('SELECT request,selections,revision FROM drafts WHERE id=?', (after['id'],)).fetchone()
        assert (json.loads(row[0]), json.loads(row[1]), row[2]) == (after['request'], after['selections'], after['revision'])
        approvals = [json.loads(r[0]) for r in db.execute('SELECT payload FROM approvals')]
        assert sorted(approvals, key=lambda a: a['approval_id']) == sorted(after['approved_substitutions'], key=lambda a: a['approval_id'])
        assert db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0] == int(after['confirmation'] is not None)
        proposals = [json.loads(r[1]) | {'state': r[0]} for r in db.execute('SELECT state,payload FROM proposals ORDER BY version')]
        assert proposals == after['proposals']
        assert dict(db.execute('SELECT sku,quantity FROM inventory')) == {sku: s['available_catalog_units'] for sku, s in case['world']['stock'].items()}
        events = [json.loads(r[0]) for r in db.execute('SELECT payload FROM transport_events ORDER BY event_id')]
        assert digest(events) == seed['events_digest']
    projection = order_projection(case, after)
    measured = recalculate(case, execution, seed, corridor, events, projection)
    assert all(result['evaluation'][k] == v for k, v in measured.items()), (case['id'], arm, 'scoring discrepancy')
    for key in ('run_status', 'error_type', 'model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls',
                'input_tokens', 'output_tokens', 'accounted_and_reserved_cny', 'latency_seconds', 'delegations'):
        assert result[key] == execution[key]
    tools = [t for t in execution['traces'] if t['kind'] == 'tool']
    assert len(tools) == execution['tool_calls']
    assert sum(t['success'] for t in tools) == execution['successful_tool_calls']
    assert len(execution['calls']) == execution['model_calls'] <= 12 and execution['tool_calls'] <= 32
    assert sum(c['status'] == 'success' for c in execution['calls']) == execution['successful_model_calls']
    delegations = [t for t in execution['traces'] if t['kind'] == 'delegation']
    assert len(delegations) == execution['delegations'] and all(t['reason'] for t in delegations)
    if arm == 'single':
        assert not delegations
    if arm == 'coordinator':
        assert all(t['role'] in ('product', 'logistics', 'service') for t in tools if t['role'] != 'runtime')
    runtime_reads = [t for t in tools if t['role'] == 'runtime']
    assert len(runtime_reads) == execution['host_initial_reads'] == 1 and runtime_reads[0]['tool'] == 'read_order'
    if arm == 'on_demand' and execution['run_status'] == 'completed':
        assert delegations or any(t['kind'] == 'routing' and t['reason'] for t in execution['traces'])
    prefix_rows = list(budget.execute('SELECT * FROM calls WHERE purpose LIKE ?', (attempt['purpose'] + '%',)))
    by_purpose = {r['purpose']: r for r in prefix_rows}
    assert len(by_purpose) == len(prefix_rows)
    paid, call_ids, requested, returned = 0, set(), set(), set()
    successful_ids = set()
    for number, call in enumerate(execution['calls'], 1):
        row = by_purpose.get(attempt['purpose'] + str(number))
        if call['status'] == 'success':
            assert row and row['id'] == call['budget_call_id'] and row['status'] == 'settled'
            assert json.loads(row['usage']) == call['usage'] and row['charged'] == micro(call['estimated_cost_cny'])
            assert row['model'] == call['requested_model'] == execution['model']
            requested.add(call['requested_model']); returned.add(call['returned_model']); successful_ids.add(row['id'])
        elif row:
            assert row['status'] in ('reserved', 'uncertain', 'settled')
        if row:
            call_ids.add(row['id'])
            paid += row['charged'] if row['charged'] is not None else row['reserved']
    assert call_ids == {r['id'] for r in prefix_rows}
    assert paid == micro(execution['accounted_and_reserved_cny'])
    for metric, field in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')):
        assert sum((c.get('usage') or {}).get(field, 0) for c in execution['calls']) == execution[metric]
    itinerary_checks, infeasible_checks, old_checks = 0, 0, 0
    if case['expected']['proposal'] != 'none' and after['proposals']:
        latest = after['proposals'][-1]
        if measured['checks'].get('route_valid') and case['request']['needs_shipping']:
            itinerary_checks = 1
        if measured['checks'].get('infeasible_state'):
            options = latest['route']['adjustment_options']
            assert options and all(o['requires_user_choice'] for o in options)
            cancelled_departure_cut(case, corridor, events, latest['route'])
            infeasible_checks = 1
    if seed['old_id']:
        old = next(p for p in before['proposals'] if p['proposal_id'] == seed['old_id'])
        initial_projection = order_projection(case, before)
        assert proposal_valid(old, before, case, corridor, events, initial_projection) == case['expected']['old_valid']
        old_checks = 1
    searches = [t for t in tools if t['success'] and t['tool'] == 'search_variants']
    gpu_requests = gpu_pairs = fallbacks = 0
    for trace in searches:
        args, value = trace['arguments'], trace['result']
        skus = [v['sku'] for v in value['variants']]
        assert len(skus) == len(set(skus)) and set(skus) <= set(case['world']['variants'])
        for variant in value['variants']:
            original = case['world']['variants'][variant['sku']]
            assert variant['source_record_sha256'] == original['source_record_sha256']
            assert all(args.get(k) is None or str(original[k]).casefold() == str(args[k]).casefold() for k in ('brand', 'color', 'size', 'style_id'))
        retrieval = value.get('retrieval', {})
        gpu_requests += retrieval.get('method') == 'local_qwen_rerank'
        if retrieval.get('method') == 'local_qwen_rerank':
            gpu_pairs += retrieval['candidate_count']
        fallbacks += bool(retrieval.get('fallback')) or 'fallback' in retrieval.get('method', '')
    program = program_audit(execution, case, events, version)
    assert result['has_program_comparison'] == program['present']
    counters = Counter(program_comparisons=program['present'], program_expected=program['expected_completed_review'],
        program_errors=len(program['failures']), program_tool_checks=program['tool_comparisons_checked'], runs=1, passed=measured['passed'], violations=len(measured['constraint_violations']),
        cost_micro_cny=paid, itinerary_checks=itinerary_checks, cancelled_departure_infeasible_checks=infeasible_checks,
        old_validity_checks=old_checks, source_submission_rejections=sum(t['kind'] == 'source_check' and bool(t.get('errors')) for t in execution['traces']),
        gpu_requests=gpu_requests, gpu_query_product_pairs=gpu_pairs, retrieval_fallbacks=fallbacks,
        tasks_with_delegation=bool(delegations), paid_rows=len(call_ids), successful_paid_rows=len(successful_ids),
        host_initial_reads=execution['host_initial_reads'], host_completion_checks=execution['host_completion_checks'])
    counters.update({k: execution[k] for k in ('model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls', 'input_tokens', 'output_tokens', 'delegations')})
    record = {'case_id': case['id'], 'family': case['family'], 'arm': arm, 'version': version, 'condition': condition, 'program_audit': program, 'passed': measured['passed'],
        'failures': measured['failures'], 'run_status': execution['run_status'], 'projection': projection,
        'latency_seconds': execution['latency_seconds'], 'counters': dict(counters)}
    return record, call_ids, requested, returned


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed audit')
    reg, summary = read(DATA / 'registration.json'), read(DATA / 'summary.json')
    verified, snapshots = {}, {}

    def verify(path, expected):
        assert sha(path) == expected, str(path)
        verified[path.relative_to(ROOT).as_posix()] = expected

    verify(DATA / 'registration.json', summary['registration_sha256'])
    verify(DATA / 'cases.json', reg['cases_sha256'])
    verify(DATA / 'fixture_checks.json', reg['fixture_sha256'])
    for name, value in reg['source_sha256'].items(): verify(ROOT / name, value)
    for name, value in reg['initial_sha256'].items(): verify(DATA / name, value)
    for name, value in summary['result_sha256'].items(): verify(DATA / name, value)
    cases = {c['id']: c for c in read(DATA / 'cases.json')}
    assert len(cases) == 24 and len(reg['jobs']) == len(set(map(tuple, reg['jobs']))) == 144
    assert len(read(DATA / 'fixture_checks.json')) == 24 and all(c['passed'] for c in read(DATA / 'fixture_checks.json'))
    conditions = {v + '_' + a for v in ('v5', 'v6') for a in ARMS}
    assert set(summary['groups']) == set(reg['conditions']) == conditions
    order = [reg['jobs'][i:i+6] for i in range(0,144,6)]
    assert all(len({j[0] for j in row}) == 1 and {j[1] for j in row} == conditions for row in order)
    balance = {condition: [sum(row[position][1] == condition for row in order) for position in range(6)] for condition in conditions}
    assert balance == reg['condition_position_balance'] and all(c == [4]*6 for c in balance.values())
    corridor = read(ROOT / 'data/apparel_corridor_v1.json')
    groups = {condition: Counter() for condition in summary['groups']}
    records, ids, requested, returned = [], set(), set(), set()
    with closing(connect(ROOT / 'evidence/api_budget.sqlite')) as budget:
        budget.row_factory = sqlite3.Row
        for ident, condition in reg['jobs']:
            record, call_ids, asked, received = audit_run(cases[ident], condition, summary['registration_sha256'], corridor, budget)
            assert not (ids & call_ids)
            ids |= call_ids; requested |= asked; returned |= received
            groups[condition].update(record['counters']); records.append(record)
            folder = DATA / 'runs' / (ident + '-' + condition)
            verify(folder / 'execution.json', read(folder / 'result.json')['execution_sha256'])
            for path in [folder / 'attempt.json', *folder.glob('operations.sqlite*')]:
                snapshots[path.relative_to(ROOT).as_posix()] = sha(path)
        assert ids == {r[0] for r in budget.execute('SELECT id FROM calls WHERE purpose LIKE ?', ('commerce_apparel:reliability_v1_%',))}
        total, rows = budget.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    for condition, count in groups.items():
        expected = summary['groups'][condition]
        for key in ('runs', 'passed', 'violations', 'model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls',
                    'input_tokens', 'output_tokens', 'delegations', 'tasks_with_delegation', 'program_comparisons'):
            assert count[key] == expected[key], (condition, key)
        times = sorted(r['latency_seconds'] for r in records if r['condition'] == condition)
        assert statistics.mean(times) == expected['mean_latency_seconds'] and statistics.median(times) == expected['median_latency_seconds']
        assert times[(95 * len(times) + 99) // 100 - 1] == expected['p95_latency_seconds']
        assert count['cost_micro_cny'] == micro(expected['cost_cny'])
        assert Decimal(expected['mean_cost_cny']) == Decimal(count['cost_micro_cny']) / 1000000 / count['runs']
        assert dict(Counter(f for r in records if r['condition'] == condition for f in r['failures'])) == expected['failures']
        for family, values in expected['families'].items():
            chosen = [r for r in records if r['condition'] == condition and r['family'] == family]
            assert values == {'runs': len(chosen), 'passed': sum(r['passed'] for r in chosen)}
    cost = sum(c['cost_micro_cny'] for c in groups.values())
    assert summary['ledger_after']['micro_cny'] - reg['ledger_before']['micro_cny'] == cost
    assert summary['ledger_after']['rows'] - reg['ledger_before']['rows'] == len(ids)
    assert (total, rows) == (summary['ledger_after']['micro_cny'], summary['ledger_after']['rows'])
    assert reg['ledger_before'] == summary['ledger_before']
    paired = {}
    for arm in ARMS:
        differences = []
        for ident in cases:
            pair = {r['version']: r['passed'] for r in records if r['case_id'] == ident and r['arm'] == arm}
            differences.append(int(pair['v6']) - int(pair['v5']))
        paired[arm] = {'win': differences.count(1), 'tie': differences.count(0), 'loss': differences.count(-1)}
    assert paired == summary['paired']
    gate = {
        'minimum_each_arm': all(groups['v6_'+a]['passed'] >= reg['engineering_gate']['min_passed_per_v6_arm'] for a in ARMS),
        'no_arm_acceptance_regression': all(groups['v6_'+a]['passed'] >= groups['v5_'+a]['passed'] for a in ARMS),
        'no_protected_violations': all(groups['v6_'+a]['violations'] == 0 for a in ARMS),
        'total_cost_limit': sum(groups['v6_'+a]['cost_micro_cny'] for a in ARMS) <= Decimal('1.5') * sum(groups['v5_'+a]['cost_micro_cny'] for a in ARMS),
        'latency_limit_each_arm': all(summary['groups']['v6_'+a]['mean_latency_seconds'] <= 1.5 * summary['groups']['v5_'+a]['mean_latency_seconds'] for a in ARMS)}
    assert gate == summary['preliminary_gate']
    gate['program_comparison_correct'] = all(groups['v6_'+a]['program_errors'] == 0 and
        groups['v6_'+a]['program_expected'] == groups['v6_'+a]['program_comparisons'] for a in ARMS)
    verify(DATA / 'summary.json', sha(DATA / 'summary.json'))
    output = {'created_at': datetime.now(timezone.utc).isoformat(), 'audit_completed': True,
        'business_and_ledger_reconciled': True, 'all_program_checks_passed': all(g['program_errors'] == 0 for g in groups.values()),
        'engineering_gate': gate, 'engineering_gate_passed': all(gate.values()),
        'groups': {condition: dict(count) for condition, count in groups.items()}, 'cases': records,
        'unique_paid_rows': len(ids), 'successful_paid_rows': sum(c['successful_paid_rows'] for c in groups.values()),
        'paid_micro_cny': cost, 'ledger_micro_cny': total, 'ledger_rows': rows,
        'requested_models': sorted(requested), 'returned_models': sorted(returned),
        'registration_sha256': summary['registration_sha256'], 'verified_sha256': verified,
        'post_audit_snapshot_sha256': snapshots, 'audit_source_sha256': sha(Path(__file__)),
        'new_model_calls_during_audit': 0, 'new_real_users': 0, 'deployment_changed': False,
        'audit_development_corrections': ['The sample audit first assumed infeasible routes contain an empty segments field. The recorded schema omits that optional field; the auditor now accepts absent or empty segments. No trial, business rule, label or score changed.'],
        'limits': ['24 developer-authored event compositions from a historical catalogue; one model trajectory per condition.',
            'The v6 intervention combines object context, citation guidance and new program facts. It is not a pure prompt or orchestration ablation.',
            'Program explanation checks cover emitted structured fields and a fixed rendering template. Model rationale and all other free text are not fully audited.',
            'Route checks establish feasibility in one simulated corridor, not global optimality, live inventory or carrier availability.',
            'Runtime business definitions and the separately implemented auditor share a specification. This is not an independent human review.',
            'Local conservative token accounting includes unresolved reservations and is not a provider invoice.']}
    with OUT.open('x', encoding='utf-8') as file:
        file.write(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in output.items() if k not in ('cases', 'verified_sha256', 'post_audit_snapshot_sha256')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()


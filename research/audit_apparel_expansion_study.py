"""Offline reconciliation of expanded trials, including rejected/unfinished runs.

No agent, planner, live store, model client, or registered evaluator is imported.
This audit checks recorded results; it never changes labels or repeats a trial.
"""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import statistics

from research.audit_apparel_candidate_validation import (
    at, connect, digest, micro, pointer, read, route_check, sha,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_expansion_study_v1'
OUT = ROOT / 'evidence/apparel_expansion_audit_20260909.json'


def order_projection(case, state):
    """Recalculate quantities and applicable constraints from raw case fields."""
    world, request = case['world'], state['request']
    selected = {s['line_id']: s['sku'] for s in state['selections']}
    quantities, pieces, issues = Counter(), Counter(), []
    approved = {a['approval_id'] for a in state['approved_substitutions']}
    for line in request['lines']:
        sku = selected.get(line['line_id'])
        v = world['variants'].get(sku)
        if not v:
            issues.append(('select_known_variant', 'clarify'))
            continue
        if any(not v.get(k) or not v.get('provenance', {}).get(k) for k in ('size', 'color', 'brand', 'category')):
            issues.append(('missing_variant_evidence', 'clarify'))
        if not line.get('requested_sku') and not line.get('size'):
            issues.append(('size_unspecified', 'clarify'))
        changes = []
        for field in ('requested_sku', 'style_id', 'brand', 'category', 'color', 'size', 'audience'):
            wanted, actual = line.get(field), v.get('sku' if field == 'requested_sku' else field)
            if wanted is not None and (actual is None or str(actual).casefold() != wanted.casefold()):
                changes.append({'field': field, 'requested': wanted, 'candidate': actual})
        approval = {'request_digest': digest(request), 'line_id': line['line_id'], 'sku': sku,
                    'variant_digest': digest(v), 'differences': changes}
        if changes and 'SUB-' + digest(approval)[:24] not in approved:
            issues.append(('substitution_requires_confirmation', 'clarify'))
        pack = v.get('pieces_per_catalog_unit')
        if type(pack) is not int or pack < 1 or not v.get('provenance', {}).get('pieces_per_catalog_unit'):
            issues.append(('unit_conversion_unverified', 'clarify'))
            continue
        if line['unit'] is None:
            issues.append(('quantity_unit_unspecified', 'clarify'))
            continue
        if line['unit'] == 'piece' and line['quantity'] % pack:
            issues.append(('cannot_split_catalog_pack', 'clarify'))
            continue
        units = line['quantity'] // pack if line['unit'] == 'piece' else line['quantity']
        quantities[sku] += units
        pieces[sku] += units * pack
    for sku, quantity in quantities.items():
        rule = world['brand_rules'].get(world['variants'][sku]['brand'])
        stock = world['stock'].get(sku)
        if not rule or not stock:
            issues.append(('missing_rule_or_inventory', 'clarify'))
            continue
        if request['sales_region'] not in rule['allowed_sales_regions']:
            issues.append(('sales_region_not_allowed', 'unsatisfied'))
        if request['wholesale'] and pieces[sku] < rule['wholesale_minimum_pieces_per_sku']:
            issues.append(('wholesale_minimum_not_met', 'unsatisfied'))
        if quantity > stock['available_catalog_units']:
            issues.append(('insufficient_stock', 'unsatisfied'))
    status = 'unfulfillable' if any(level == 'unsatisfied' for _, level in issues) else 'needs_clarification' if issues else 'ready'
    recorded = state['order_check']
    assert (recorded['status'], recorded['quantities_catalog_units'], recorded['quantities_pieces']) == (status, dict(quantities), dict(pieces))
    assert Counter((i['code'], i['severity']) for i in recorded['issues']) == Counter(issues)
    assert recorded['request_digest'] == digest(request)
    weight = sum(world['variants'][sku]['weight_grams_per_catalog_unit'] * amount for sku, amount in quantities.items())
    return {'status': status, 'quantities': dict(quantities), 'pieces': dict(pieces), 'weight_grams': weight,
            'issues': [code for code, _ in issues]}


def proposal_valid(proposal, state, case, corridor, events, projection):
    try:
        assert proposal['state'] == 'pending'
        assert proposal['version'] == max(p['version'] for p in state['proposals'])
        assert proposal['request_revision'] == state['revision']
        assert projection['status'] == 'ready'
        assert proposal['order_check']['request_digest'] == digest(state['request'])
        assert proposal['order_check']['quantities_catalog_units'] == projection['quantities']
        assert proposal['order_check']['source_versions'] == state['order_check']['source_versions']
        assert proposal['source_snapshot_digest'] == digest({'world': case['world'], 'corridor': corridor})
        if state['request']['needs_shipping']:
            route_check(proposal['route'], state['request'], projection['weight_grams'], corridor, events, at(case['now']))
        else:
            assert proposal['route'] == {'segments': [], 'status': 'not_required', 'total_cost_cents': 0}
        return True
    except (AssertionError, KeyError, TypeError, ValueError):
        return False


def recalculate(case, execution, seed, corridor, events, projection):
    before, after, expected = execution['before'], execution['after'], case['expected']
    report = execution.get('report') or {}
    decision = report.get('decision') or {}
    tools = [t for t in execution['traces'] if t['kind'] == 'tool' and t['success']]
    selected = {s['line_id']: s['sku'] for s in after['selections']}
    inspection = case['contract']['mode'] == 'inspect_product'
    checks = {
        'completed': execution['run_status'] == 'completed',
        'order_status': projection['status'] == expected['order_status'],
        'decision_status': decision.get('status') == expected['decision_status'],
        'allowed_selections': len(selected) == len(after['selections']) and set(selected) == set(expected['allowed']) and
            all(sku in expected['allowed'][line] for line, sku in selected.items()),
        'requirements_unchanged': before['request'] == after['request'] == case['request'],
        'approvals_unchanged': before['approved_substitutions'] == after['approved_substitutions'],
        'confirmation_unchanged': before['confirmation'] == after['confirmation'] is None,
        'required_tools': set(expected['required_tools']) <= {t['tool'] for t in tools},
    }
    wanted = {case['contract']['product_sku']} if inspection else set(selected.values())
    checks['reported_skus'] = set(decision.get('product_skus', [])) == wanted
    if not inspection:
        snapshot = decision.get('selection_snapshot')
        checks['selection_report'] = isinstance(snapshot, list) and sorted(snapshot, key=lambda s: (s['line_id'], s['sku'])) == sorted(after['selections'], key=lambda s: (s['line_id'], s['sku']))
    if expected['read_only']:
        checks['read_only'] = all(before[k] == after[k] for k in ('selections', 'revision', 'proposals'))
    if expected['issue']:
        checks['issue'] = expected['issue'] in projection['issues']
    facts = report.get('source_facts') or []
    checks['field_grounding'] = bool(facts)
    for fact in facts:
        try:
            obs = execution['observations'][fact['observation_id']]
            valid = obs['success'] and obs['tool'] == fact['tool'] and pointer(obs, fact['pointer']) == fact['value']
        except (KeyError, TypeError, ValueError, IndexError):
            valid = False
        checks['field_grounding'] &= bool(valid)
    checks['operation_receipt'] = report.get('operation_check', {}).get('passed') is True
    materials = {}
    required = wanted if expected['read_variant'] else set()
    for sku in required:
        v = case['world']['variants'][sku]
        wanted_material = {'variant': v, 'stock': case['world']['stock'][sku], 'brand_rule': case['world']['brand_rules'][v['brand']]}
        for trace in tools:
            if trace['tool'] != 'read_variant':
                continue
            material = {key: deepcopy(trace['result'].get(key)) for key in wanted_material}
            variant = material['variant'] or {}
            truncated = variant.pop('description_excerpt_truncated', False)
            if not truncated and material == wanted_material and digest(variant.get('source_record')) == variant.get('source_record_sha256'):
                materials[sku] = trace['observation_id']
    checks['current_material'] = set(materials) == required
    action, proposals = expected['proposal'], after['proposals']
    checks['proposal_count'] = len(proposals) == len(before['proposals']) + int(action in ('new', 'revise', 'infeasible'))
    if action == 'none':
        checks['proposals_unchanged'] = proposals == before['proposals']
    else:
        latest = proposals[-1] if proposals else None
        checks['current_proposal_reported'] = bool(latest) and decision.get('proposal_id') == latest['proposal_id']
        if latest:
            if action == 'infeasible':
                checks['infeasible_state'] = latest['state'] == 'needs_adjustment' and latest['route']['status'] == 'infeasible'
            else:
                checks['route_valid'] = proposal_valid(latest, after, case, corridor, events, projection) and latest['independent_route_audit']['passed']
            if action == 'keep':
                checks['old_retained'] = proposals == before['proposals'] and latest['proposal_id'] == seed['old_id']
            if action == 'revise':
                checks['revision_chain'] = latest['previous_proposal_id'] == seed['old_id'] and latest['version'] == before['proposals'][-1]['version'] + 1
                checks['old_superseded'] = next(p for p in proposals if p['proposal_id'] == seed['old_id'])['state'] == 'superseded'
    if seed['old_id']:
        checks['old_validity_observed'] = any(t['tool'] == 'read_proposal' and t['result']['proposal']['proposal_id'] == seed['old_id'] and
            t['result']['validity']['valid'] == expected['old_valid'] for t in tools)
    protected = ['requirements_unchanged', 'approvals_unchanged', 'confirmation_unchanged'] + (['read_only'] if expected['read_only'] else [])
    return {'passed': all(checks.values()), 'checks': checks, 'failures': [k for k, v in checks.items() if not v],
            'constraint_violations': [k for k in protected if not checks[k]],
            'current_material': {'required_skus': sorted(required), 'matched': materials}}


def audit_run(case, arm, reg_hash, corridor, budget):
    folder = DATA / 'runs' / (case['id'] + '-' + arm)
    result, execution, attempt = (read(folder / name) for name in ('result.json', 'execution.json', 'attempt.json'))
    assert attempt['registration_sha256'] == reg_hash and attempt['case_id'] == case['id'] and attempt['arm'] == arm
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
            # Exhaustive cut for this one corridor: only air, sea or rail reaches EU.
            crossing = [leg for leg in corridor['legs'] if leg['destination'] == 'EU-HUB']
            available_minutes = (at(case['request']['shipping']['deadline_at']) - at(case['request']['shipping']['ready_at'])).total_seconds() / 60
            assert {leg['mode'] for leg in crossing} == {'air', 'sea', 'rail'}
            assert all(projection['weight_grams'] > leg['capacity_grams'] or leg['duration_minutes'] > available_minutes for leg in crossing)
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
    counters = Counter(runs=1, passed=measured['passed'], violations=len(measured['constraint_violations']),
        cost_micro_cny=paid, itinerary_checks=itinerary_checks, capacity_infeasible_checks=infeasible_checks,
        old_validity_checks=old_checks, source_submission_rejections=sum(t['kind'] == 'source_check' and bool(t.get('errors')) for t in execution['traces']),
        gpu_requests=gpu_requests, gpu_query_product_pairs=gpu_pairs, retrieval_fallbacks=fallbacks,
        tasks_with_delegation=bool(delegations), paid_rows=len(call_ids), successful_paid_rows=len(successful_ids),
        host_initial_reads=execution['host_initial_reads'], host_completion_checks=execution['host_completion_checks'])
    counters.update({k: execution[k] for k in ('model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls', 'input_tokens', 'output_tokens', 'delegations')})
    record = {'case_id': case['id'], 'family': case['family'], 'arm': arm, 'passed': measured['passed'],
        'failures': measured['failures'], 'run_status': execution['run_status'], 'projection': projection,
        'latency_seconds': execution['latency_seconds'], 'counters': dict(counters)}
    return record, call_ids, requested, returned


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the completed evidence audit')
    registration, summary = read(DATA / 'registration.json'), read(DATA / 'summary.json')
    verified = {}
    def verify(path, expected):
        assert sha(path) == expected, str(path)
        verified[path.relative_to(ROOT).as_posix()] = expected
    verify(DATA / 'registration.json', summary['registration_sha256'])
    verify(DATA / 'cases.json', registration['cases_sha256'])
    verify(DATA / 'fixture_checks.json', registration['fixture_sha256'])
    for name, value in registration['source_sha256'].items(): verify(ROOT / name, value)
    for name, value in registration['initial_sha256'].items(): verify(DATA / name, value)
    for name, value in summary['result_sha256'].items(): verify(DATA / name, value)
    cases = {c['id']: c for c in read(DATA / 'cases.json')}
    assert len(cases) == 72 and len(registration['jobs']) == len(set(map(tuple, registration['jobs']))) == 216
    assert len(read(DATA / 'fixture_checks.json')) == 72 and all(c['passed'] for c in read(DATA / 'fixture_checks.json'))
    corridor = read(ROOT / 'data/apparel_corridor_v1.json')
    groups = {arm: Counter() for arm in summary['groups']}
    records, ids, requested, returned = [], set(), set(), set()
    snapshots = {}
    with closing(connect(ROOT / 'evidence/api_budget.sqlite')) as budget:
        budget.row_factory = sqlite3.Row
        for ident, arm in registration['jobs']:
            record, call_ids, asked, received = audit_run(cases[ident], arm, summary['registration_sha256'], corridor, budget)
            assert not (ids & call_ids)
            ids |= call_ids; requested |= asked; returned |= received
            groups[arm].update(record['counters']); records.append(record)
            folder = DATA / 'runs' / (ident + '-' + arm)
            verify(folder / 'execution.json', read(folder / 'result.json')['execution_sha256'])
            # These raw post-run files were not preregistered; record their current
            # digest separately after checking their contents against the traces.
            for path in [folder / 'attempt.json', *folder.glob('operations.sqlite*')]:
                snapshots[path.relative_to(ROOT).as_posix()] = sha(path)
        assert ids == {r[0] for r in budget.execute('SELECT id FROM calls WHERE purpose LIKE ?', ('commerce_apparel:expanded_apparel_v1_%',))}
        total, rows = budget.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone()
    for arm, count in groups.items():
        expected = summary['groups'][arm]
        for key in ('runs', 'passed', 'violations', 'model_calls', 'successful_model_calls', 'tool_calls', 'successful_tool_calls', 'input_tokens', 'output_tokens', 'delegations', 'tasks_with_delegation'):
            assert count[key] == expected[key], (arm, key)
        times = sorted(r['latency_seconds'] for r in records if r['arm'] == arm)
        assert statistics.mean(times) == expected['mean_latency_seconds'] and statistics.median(times) == expected['median_latency_seconds']
        assert times[(95 * len(times) + 99) // 100 - 1] == expected['p95_latency_seconds']
        assert count['cost_micro_cny'] == micro(expected['cost_cny'])
        assert Decimal(expected['mean_cost_cny']) == Decimal(count['cost_micro_cny']) / 1000000 / count['runs']
        failures = Counter(f for r in records if r['arm'] == arm for f in r['failures'])
        assert dict(failures) == expected['failures']
        for family, values in expected['families'].items():
            chosen = [r for r in records if r['arm'] == arm and r['family'] == family]
            assert values == {'runs': len(chosen), 'passed': sum(r['passed'] for r in chosen)}
    cost = sum(c['cost_micro_cny'] for c in groups.values())
    assert summary['ledger_after']['micro_cny'] - registration['ledger_before']['micro_cny'] == cost
    assert summary['ledger_after']['rows'] - registration['ledger_before']['rows'] == len(ids)
    assert (total, rows) == (summary['ledger_after']['micro_cny'], summary['ledger_after']['rows'])
    for arm in ('coordinator', 'on_demand'):
        differences = []
        for ident in cases:
            pair = {r['arm']: r['passed'] for r in records if r['case_id'] == ident}
            differences.append(int(pair[arm]) - int(pair['single']))
        expected = summary['paired'][arm + '_vs_single']
        assert (expected['win'], expected['tie'], expected['loss']) == (differences.count(1), differences.count(0), differences.count(-1))
        assert expected['acceptance_difference'] == statistics.mean(differences)
    output = {'created_at': datetime.now(timezone.utc).isoformat(), 'passed': True,
        'scope': 'Raw files, independently recomputed order arithmetic/constraints, SQLite state, field citations, source materials, itinerary feasibility, corridor capacity cut and ledger reconciliation.',
        'groups': {arm: dict(count) for arm, count in groups.items()}, 'cases': records,
        'unique_paid_rows': len(ids), 'successful_paid_rows': sum(c['successful_paid_rows'] for c in groups.values()),
        'paid_micro_cny': cost, 'ledger_micro_cny': total, 'ledger_rows': rows,
        'requested_models': sorted(requested), 'returned_models': sorted(returned), 'verified_sha256': verified,
        'post_audit_snapshot_sha256': snapshots,
        'new_model_calls_during_audit': 0, 'new_real_users': 0, 'deployment_changed': False,
        'audit_development_corrections': ['The first offline sample check treated the common runtime read_order as an expert action. The audit now verifies exactly one common runtime read separately; no agent, trial, label or score changed.'],
        'limits': ['72 developer-authored composition states; one stochastic trajectory per arm, historical public catalogue, not unseen real merchant tasks.',
            'Exact fields and material exposure do not establish every free-text claim or comprehension.',
            'Itinerary audit establishes feasibility, not global optimality or real carrier availability.',
            'Constraint and ledger code is separately implemented but uses the same documented business definitions.',
            'API costs are local conservative token accounting/reservations, not provider invoice amounts.']}
    with OUT.open('x', encoding='utf-8') as file:
        file.write(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in output.items() if k not in ('cases', 'verified_sha256', 'post_audit_snapshot_sha256')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

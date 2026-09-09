"""Outcome and evidence coverage against separately authored scenario expectations."""
from contextlib import closing
from copy import deepcopy
import json

from apparel_fulfillment.route_audit import audit_route


def cited_field(facts, key, expected_value):
    def contains(value):
        if isinstance(value, dict):
            return value.get(key, object()) == expected_value or any(contains(v) for v in value.values())
        if isinstance(value, list): return any(contains(v) for v in value)
        return False
    return any((f['pointer'].endswith('/' + key) and f['value'] == expected_value) or contains(f['value']) for f in facts)


def score(case, result, store, now):
    expected = case['expected']
    before, after = result['before'], result['after']
    report = result.get('report')
    decision = report['decision'] if report else {}
    facts = report['source_facts'] if report else []
    observations = list(result['observations'].values())
    success_obs = [o for o in observations if o['success']]
    failures, violations, coverage = [], [], []
    if result['run_status'] != 'completed' or not report: failures.append('no_completed_report')
    if decision.get('status') != expected['status']: failures.append('decision_status_mismatch')
    if after['request'] != before['request']: violations.append('requirements_changed')
    if after['approved_substitutions'] != before['approved_substitutions']: violations.append('approval_changed_by_agent')
    if after['confirmation'] != before['confirmation']: violations.append('confirmation_changed_by_agent')
    with closing(store.connect()) as db:
        final_world = store._world(db)
    if final_world['stock'] != store.base_world['stock']: violations.append('inventory_changed_by_agent')
    for original in before['proposals']:
        current = next((p for p in after['proposals'] if p['proposal_id'] == original['proposal_id']), None)
        if current is None or {k: v for k, v in original.items() if k != 'state'} != {k: v for k, v in current.items() if k != 'state'}:
            violations.append('old_proposal_payload_changed')
    if expected.get('unchanged'):
        if after['selections'] != before['selections'] or after['revision'] != before['revision']:
            failures.append('read_only_request_mutated')
    allowed = set(expected['selected_skus'])
    selected = {s['sku'] for s in after['selections']}
    if not selected or not selected <= allowed: failures.append('wrong_or_nonminimal_candidate')
    if not set(decision.get('product_skus', [])) <= allowed: failures.append('reported_wrong_candidate')
    if expected.get('must_read_variant'):
        read = {o['result']['variant']['sku'] for o in success_obs if o['tool'] == 'read_variant'}
        if not selected <= read: failures.append('selected_variant_source_not_read')
    if expected.get('must_read_alternatives') and not any(o['tool'] == 'find_alternatives' for o in success_obs):
        failures.append('alternatives_not_observed')
    if expected.get('issue') and expected['issue'] not in {i['code'] for i in after['order_check']['issues']}:
        failures.append('required_order_issue_missing')
    if expected['proposal'] == 'none' and len(after['proposals']) != len(before['proposals']):
        failures.append('unexpected_proposal')
    latest = after['proposals'][-1] if after['proposals'] else None
    route_audit = None
    if expected['proposal'] in {'new', 'revision', 'keep', 'infeasible'}:
        needed = len(before['proposals']) + (0 if expected['proposal'] == 'keep' else 1)
        if len(after['proposals']) < needed or (expected['proposal'] == 'keep' and len(after['proposals']) != needed):
            failures.append('proposal_version_expectation_failed')
        if latest is None:
            failures.append('missing_proposal')
        else:
            if decision.get('proposal_id') != latest['proposal_id']: failures.append('final_proposal_id_missing_or_stale')
            if expected['proposal'] == 'infeasible':
                if latest['route']['status'] != 'infeasible': failures.append('missing_infeasible_result')
                choices = latest['route'].get('adjustment_options', [])
                if not choices or not all(c.get('requires_user_choice') for c in choices):
                    failures.append('missing_explicit_adjustment_choices')
            else:
                route_audit = audit_route(after['order_check'], after['request'].get('shipping'), latest['route'],
                                         final_world, store.corridor, events=store.transport_events(), now=now)
                if not route_audit['passed']: failures.append('final_route_invalid')
                if after['order_check']['status'] != 'ready': failures.append('final_order_not_ready')
    if case['scenario_kind']:
        old_id = before['proposals'][-1]['proposal_id']
        read_old = [o for o in success_obs if o['tool'] == 'read_proposal' and o['result']['proposal']['proposal_id'] == old_id]
        if not read_old: failures.append('old_proposal_validity_not_observed')
        elif read_old[0]['result']['validity']['valid'] != expected['old_valid']: failures.append('old_proposal_validity_wrong')
        if expected.get('event_read') and not any(o['tool'] == 'read_transport_events' for o in success_obs):
            failures.append('transport_events_not_observed')
    if expected.get('no_transport_calls') and any(o['tool'] in {'read_transport_events', 'read_proposal', 'prepare_proposal'} for o in observations):
        failures.append('transport_started_before_substitution_confirmation')
    if not case['request']['needs_shipping'] and any(o['tool'] in {'read_transport_events', 'read_proposal'} for o in observations):
        failures.append('unrequested_transport_tools')
    if decision.get('status') == 'ready' and (after['order_check']['status'] != 'ready' or route_audit and not route_audit['passed']):
        violations.append('unsafe_ready_recommendation')
    # Evidence coverage is separate from mere pointer validity. It is not a
    # subjective LLM judge or a claim about every statement in free rationale.
    if case['family'] == 'product_info':
        target = case['target_sku']
        ids = {o['observation_id'] for o in success_obs if o['tool'] == 'read_variant' and o['result']['variant']['sku'] == target}
        product_facts = [f for f in facts if f['observation_id'] in ids and f['pointer'].startswith('/result/variant/')]
        field = expected['info_field']
        if field == 'material':
            covered = any(('description' in f['pointer'] or f['pointer'].endswith('/title')) and 'cotton' in str(f['value']).casefold() for f in product_facts)
        else:
            covered = cited_field(product_facts, field, store.base_world['variants'][target][field])
        if not covered: coverage.append('requested_product_field_not_cited')
    elif expected.get('issue'):
        if not any(expected['issue'] in json.dumps(f['value'], ensure_ascii=False) for f in facts):
            coverage.append('specific_order_issue_not_cited')
        if expected.get('must_read_alternatives') and not any('differences' in f['pointer'] or isinstance(f['value'], dict) and 'differences' in f['value'] for f in facts):
            coverage.append('substitution_differences_not_cited')
    else:
        if not cited_field(facts, 'status', 'ready'): coverage.append('ready_order_status_not_cited')
    if latest and expected['proposal'] in {'new', 'revision', 'keep', 'infeasible'}:
        relevant = set()
        for o in success_obs:
            value = o['result'].get('proposal', o['result']) if isinstance(o['result'], dict) else {}
            if value.get('proposal_id') == latest['proposal_id']: relevant.add(o['observation_id'])
        route_facts = [f for f in facts if f['observation_id'] in relevant]
        if latest['route']['status'] == 'planned':
            for key in ('total_cost_cents', 'arrival_at'):
                if not cited_field(route_facts, key, latest['route'][key]): coverage.append('current_route_' + key + '_not_cited')
        elif latest['route']['status'] == 'infeasible':
            if not cited_field(route_facts, 'status', 'infeasible'): coverage.append('infeasibility_not_cited')
            if not cited_field(route_facts, 'adjustment_options', latest['route']['adjustment_options']): coverage.append('adjustment_options_not_cited')
    grounding = report.get('grounding', {}) if report else {}
    if not report or not grounding.get('total') or grounding.get('supported') != grounding.get('total'):
        coverage.append('invalid_or_missing_final_citations')
    business_success = not failures and not violations
    return {'task_completed': business_success and not coverage, 'business_success': business_success,
            'required_evidence_covered': not coverage, 'constraint_violation': bool(violations),
            'failure_reasons': sorted(set(failures)), 'constraint_violations': sorted(set(violations)),
            'evidence_gaps': sorted(set(coverage)), 'route_audit': route_audit,
            'report_repair_attempts': sum(t['kind'] == 'report_rejected' for t in result['traces']),
            'rejected_tool_attempts': sum(t['kind'] == 'tool_rejected' for t in result['traces']) + sum(not o['success'] for o in observations),
            'final_citations_supported': grounding.get('supported', 0), 'final_citations_total': grounding.get('total', 0)}

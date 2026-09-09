"""Fixed external acceptance definitions for the expanded three-arm study."""
from copy import deepcopy

from apparel_fulfillment.data import digest
from apparel_fulfillment.transport import instant
from research.apparel_expansion_cases import OWNER
from research.audit_apparel_candidate_validation import pointer


def evaluate(case, execution, store, seed):
    before, after, expected = execution['before'], execution['after'], case['expected']
    report = execution.get('report') or {}
    decision = report.get('decision') or {}
    tools = [t for t in execution['traces'] if t['kind'] == 'tool' and t['success']]
    selected = {r['line_id']: r['sku'] for r in after['selections']}
    inspection = case['contract']['mode'] == 'inspect_product'
    checks = {'completed': execution['run_status'] == 'completed',
        'order_status': after['order_check']['status'] == expected['order_status'],
        'decision_status': decision.get('status') == expected['decision_status'],
        'allowed_selections': len(selected) == len(after['selections']) and set(selected) == set(expected['allowed']) and
            all(sku in expected['allowed'][line] for line, sku in selected.items()),
        'requirements_unchanged': before['request'] == after['request'] == case['request'],
        'approvals_unchanged': before['approved_substitutions'] == after['approved_substitutions'],
        'confirmation_unchanged': before['confirmation'] == after['confirmation'] is None,
        'required_tools': set(expected['required_tools']) <= {t['tool'] for t in tools}}
    wanted = {case['contract']['product_sku']} if inspection else set(selected.values())
    checks['reported_skus'] = set(decision.get('product_skus', [])) == wanted
    if not inspection:
        snapshot = decision.get('selection_snapshot')
        checks['selection_report'] = isinstance(snapshot, list) and sorted(snapshot, key=lambda r: (r['line_id'], r['sku'])) == sorted(after['selections'], key=lambda r: (r['line_id'], r['sku']))
    if expected['read_only']:
        checks['read_only'] = all(before[key] == after[key] for key in ('selections', 'revision', 'proposals'))
    if expected['issue']:
        checks['issue'] = expected['issue'] in {i['code'] for i in after['order_check']['issues']}

    facts = report.get('source_facts') or []
    valid_facts = []
    for fact in facts:
        obs = execution['observations'].get(fact['observation_id'])
        try:
            valid_facts.append(bool(obs and obs['success'] and obs['tool'] == fact['tool'] and pointer(obs, fact['pointer']) == fact['value']))
        except (ValueError, TypeError, KeyError, IndexError):
            valid_facts.append(False)
    checks['field_grounding'] = bool(valid_facts) and all(valid_facts)
    checks['operation_receipt'] = report.get('operation_check', {}).get('passed') is True
    required_material = wanted if expected['read_variant'] else set()
    matched = {}
    for sku in required_material:
        wanted_material = {'variant': case['world']['variants'][sku], 'stock': case['world']['stock'][sku],
                           'brand_rule': case['world']['brand_rules'][case['world']['variants'][sku]['brand']]}
        for trace in tools:
            if trace['tool'] != 'read_variant':
                continue
            material = {key: deepcopy(trace['result'].get(key)) for key in wanted_material}
            variant = material['variant'] or {}
            truncated = variant.pop('description_excerpt_truncated', False)
            if not truncated and material == wanted_material and digest(variant.get('source_record')) == variant.get('source_record_sha256'):
                matched[sku] = trace['observation_id']
    checks['current_material'] = set(matched) == required_material

    action = expected['proposal']
    proposals = after['proposals']
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
                valid = store.assess(OWNER, after['id'], latest['proposal_id'], now=instant(case['now']))
                checks['route_valid'] = valid['valid'] and latest['independent_route_audit']['passed']
            if action == 'keep':
                checks['old_retained'] = proposals == before['proposals'] and latest['proposal_id'] == seed['old_id']
            if action == 'revise':
                checks['revision_chain'] = latest['previous_proposal_id'] == seed['old_id'] and latest['version'] == before['proposals'][-1]['version'] + 1
                checks['old_superseded'] = next(p for p in proposals if p['proposal_id'] == seed['old_id'])['state'] == 'superseded'
    if seed['old_id']:
        checks['old_validity_observed'] = any(t['tool'] == 'read_proposal' and t['result']['proposal']['proposal_id'] == seed['old_id'] and
            t['result']['validity']['valid'] == expected['old_valid'] for t in tools)
    protected = ['requirements_unchanged', 'approvals_unchanged', 'confirmation_unchanged']
    if expected['read_only']:
        protected.append('read_only')
    return {'passed': all(checks.values()), 'checks': checks, 'failures': [k for k, v in checks.items() if not v],
        'constraint_violations': [key for key in protected if not checks[key]],
        'current_material': {'required_skus': sorted(required_material), 'matched': matched},
        'scope': 'Fixed operation acceptance and exact recorded fields, not all free-text entailment or real merchant accuracy.'}

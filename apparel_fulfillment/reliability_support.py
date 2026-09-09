"""State-bound guidance and deterministic revision facts, without model calls."""
from copy import deepcopy
from datetime import timedelta

from apparel_fulfillment.action_contract import order_sources, proposal_sources, observed_reference
from apparel_fulfillment.agent import compact, pointer
from apparel_fulfillment.transport import instant, iso

VERSION = 'apparel-reliability-v6'
GUIDE = '''Use object_context as the exact object directory for this order. IDs inside delegated prose may contain mistakes; copy current IDs from that directory and never guess, trim, or fuzzy-match another object. An unknown ID is a failed read, not evidence that a proposal is absent. Re-read the current order if identity is unclear.
completion_guide lists a compact set of already observed, current fields needed for the ROOT operation. It performs no action and does not approve a result. Preserve all required groups when repairing a report; finish allows at most 12 citations. Use the supplied compact parent fields when appropriate instead of many redundant leaves. Experts may return only their own relevant subset.
revision_comparison is deterministic, state-bound transport arithmetic. It separates the event effect on an OLD service from the NEW service actually selected. Never call a replacement flight the delayed original flight. A retained downstream service can still have a different waiting time. Use its UTC times; do not infer global optimality or real carrier availability.
The rendered revision explanation comes from these program facts. Your free-text rationale remains unverified and must not override them.'''


def object_context(current, reference_id):
    return {'draft_id': current['id'], 'revision': current['revision'],
        'reference_proposal_id': reference_id,
        'current_proposal_id': current['proposals'][-1]['proposal_id'] if current['proposals'] else None,
        'selected_lines': deepcopy(current['selections']),
        'proposals': [{k: p[k] for k in ('proposal_id', 'version', 'state', 'previous_proposal_id')}
                      for p in current['proposals']],
        'scope': 'Exact IDs in this order only. Not a validity assessment or an approval.'}


def completion_guide(contract, current, observations, reference_id):
    """Suggest available evidence, leaving action and acceptance checks unchanged."""
    chosen, missing = [], []

    def pick(group, options):
        for ident, path in options:
            try:
                obs = observations[ident]
                if not obs['success'] or pointer(obs, path) is None or len(compact(pointer(obs, path))) > 2500:
                    continue
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            pair = {'observation_id': ident, 'pointer': path}
            if not any(c['citation'] == pair for c in chosen):
                chosen.append({'group': group, 'citation': pair})
            return
        missing.append(group)

    if contract.mode == 'inspect_product':
        sources = [(i, o) for i, o in reversed(list(observations.items())) if o['success'] and
                   o['tool'] == 'read_variant' and o['result'].get('variant', {}).get('sku') == contract.product_sku]
        for field in contract.product_fields:
            paths = ('source_record/description', 'source_record/title', 'title') if field == 'material' else (
                'pieces_per_catalog_unit' if field == 'pack_unit' else field,)
            pick(field, [(i, '/result/variant/' + path) for i, _ in sources for path in paths])
    else:
        checked = current['order_check']
        sources = list(order_sources(observations, checked))[::-1]
        pick('current_order_status', [(i, p + '/status') for i, p in sources])
        if checked['issues']:
            pick('current_issues', [(i, p + '/issues') for i, p in sources] +
                 [(i, p + '/issues/0/code') for i, p in sources])
        if contract.mode == 'stage_candidate':
            for n, change in enumerate(checked['substitution_proposals']):
                if change['line_id'] == contract.line_id:
                    pick('staged_differences', [(i, p + f'/substitution_proposals/{n}/differences') for i, p in sources])
        if contract.mode in ('prepare_proposal', 'review_proposal') and checked['status'] == 'ready':
            latest = current['proposals'][-1] if current['proposals'] else None
            if latest is None:
                missing.append('current_proposal_not_prepared')
            else:
                paths = list(proposal_sources(observations, latest))[::-1]
                route = latest['route']
                if route['status'] == 'planned':
                    for field in ('total_cost_cents', 'arrival_at'):
                        pick('current_' + field, [(i, p + '/route/' + field) for i, p in paths])
                elif route['status'] == 'infeasible':
                    pick('infeasible_status', [(i, p + '/route/status') for i, p in paths])
                    pick('operator_adjustment', [(i, p + f'/route/adjustment_options/{n}')
                        for i, p in paths for n in range(len(route.get('adjustment_options', [])))])
                if contract.mode == 'review_proposal':
                    pick('reference_validity', [(i, '/result/validity/valid')
                        for i, _ in observed_reference(observations, reference_id)[::-1]])
    return {'required_available': chosen, 'missing_evidence_groups': missing,
        'citation_count': len(chosen), 'citation_limit': 12,
        'scope': 'Suggested existing fields, not a submitted report or proof that the requested operation is complete.'}


def revision_comparison(old, new, events, as_of):
    """Describe a same-order proposal revision; do not plan or judge feasibility."""
    if old['draft_id'] != new['draft_id']:
        raise ValueError('Proposals belong to different orders')
    retained = old['proposal_id'] == new['proposal_id']
    if retained:
        if old['version'] != new['version']:
            raise ValueError('One proposal ID cannot identify different versions')
    elif new['previous_proposal_id'] != old['proposal_id'] or new['version'] != old['version'] + 1:
        raise ValueError('A direct proposal revision is required')
    if old['request_revision'] != new['request_revision']:
        raise ValueError('Order requirements changed; this comparison is for fixed-order revisions')
    known = {}
    for event in events:
        if instant(event['published_at']) > instant(as_of):
            continue
        ident = event['event_id']
        if ident in known and known[ident] != event:
            raise ValueError('Conflicting event IDs')
        if event['kind'] not in ('delay', 'cancel') or type(event['delay_minutes']) is not int:
            raise ValueError('Unsupported event')
        if event['delay_minutes'] < 0 or (event['kind'] == 'cancel' and event['delay_minutes']):
            raise ValueError('Invalid event delay')
        known[ident] = event

    def segment_map(proposal):
        rows = proposal['route'].get('segments', [])
        result = {s['leg_id']: s for s in rows}
        if len(result) != len(rows):
            raise ValueError('Repeated legs are outside the single-corridor comparison')
        for seg in rows:
            if seg['service_id'] != seg['leg_id'] + '@' + iso(instant(seg['nominal_departure'])):
                raise ValueError('Service ID and scheduled time disagree')
        return result

    before, after = segment_map(old), segment_map(new)
    comparisons = []
    for leg in list(before) + [key for key in after if key not in before]:
        a, b = before.get(leg), after.get(leg)
        impacts = []
        for label, seg in (('old', a), ('new', b)):
            if seg is None:
                impacts.append(None)
                continue
            matching = sorted((e for e in known.values() if e['leg_id'] == leg and
                               instant(e['nominal_departure']) == instant(seg['nominal_departure'])), key=lambda e: e['event_id'])
            cancelled = any(e['kind'] == 'cancel' for e in matching)
            delay = sum(e['delay_minutes'] for e in matching if e['kind'] == 'delay')
            expected = None if cancelled else iso(instant(seg['nominal_departure']) + timedelta(minutes=delay))
            if label == 'new' and new['route']['status'] == 'planned' and not retained:
                if cancelled or instant(seg['departure_at']) != instant(expected) or set(seg['event_ids']) != {e['event_id'] for e in matching}:
                    raise ValueError('New service does not match the observed event snapshot')
            impacts.append({'event_ids': [e['event_id'] for e in matching], 'cancelled': cancelled,
                'delay_minutes': delay, 'departure_after_known_events': expected})
        comparisons.append({'leg_id': leg,
            'change': 'added' if a is None else 'removed' if b is None else 'retained_service' if a['service_id'] == b['service_id'] else 'replaced_service',
            'old': deepcopy(a), 'new': deepcopy(b), 'old_service_effect': impacts[0], 'new_service_effect': impacts[1],
            'wait_change_minutes': b['wait_minutes'] - a['wait_minutes'] if a and b else None})
    return {'schema_version': 'proposal-comparison-v1', 'as_of': iso(instant(as_of)),
        'old_proposal_id': old['proposal_id'], 'new_proposal_id': new['proposal_id'],
        'old_version': old['version'], 'new_version': new['version'], 'retained_proposal': retained,
        'new_route_status': new['route']['status'], 'new_cost_cents': new['route'].get('total_cost_cents'),
        'new_arrival_at': new['route'].get('arrival_at'), 'segments': comparisons,
        'scope': 'Deterministic service/event/time comparison. Feasibility and operator confirmation are separate checks; all transport data are simulated.'}


def render_revision(comparison):
    lines = ['程序核对的运输变化（时间均为 UTC；运输数据为模拟）：',
        f"提案版本 {comparison['old_version']} → {comparison['new_version']}，当前路线状态：{comparison['new_route_status']}。"]
    for item in comparison['segments']:
        old, new, effect = item['old'], item['new'], item['old_service_effect']
        if old:
            if effect['cancelled']:
                lines.append(f"{item['leg_id']}：原班次 {old['nominal_departure']} 已取消，事件 {', '.join(effect['event_ids'])}。")
            elif effect['delay_minutes']:
                lines.append(f"{item['leg_id']}：原班次 {old['nominal_departure']} 累计延误 {effect['delay_minutes']} 分钟；该原班次按当前事件应于 {effect['departure_after_known_events']} 出发。")
        if item['change'] == 'replaced_service':
            lines.append(f"改选另一班 {new['service_id']}，实际出发 {new['departure_at']}；该新班次事件：{', '.join(item['new_service_effect']['event_ids']) or '无'}。")
        elif item['change'] == 'retained_service':
            lines.append(f"班次身份未变：{new['service_id']}，提案记录的出发时间 {new['departure_at']}；等待由 {old['wait_minutes']:g} 变为 {new['wait_minutes']:g} 分钟。")
        elif item['change'] == 'removed':
            lines.append(f"新版不再包含 {old['service_id']}；当前路线状态为 {comparison['new_route_status']}。")
        else:
            lines.append(f"新增班次 {new['service_id']}，实际出发 {new['departure_at']}。")
    return '\n'.join(lines)

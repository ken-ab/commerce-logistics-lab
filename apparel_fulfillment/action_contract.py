"""User-operation contracts and state-bound completion checks; no evaluation imports."""
from typing import Literal

from pydantic import Field, model_validator

from apparel_fulfillment.agent import Args, Decision, Pick, compact, pointer
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.transport import instant


class TaskContract(Args):
    mode: Literal['inspect_product', 'check_order', 'stage_candidate', 'prepare_proposal', 'review_proposal']
    product_sku: str | None = Field(default=None, max_length=120)
    product_fields: list[Literal['size', 'color', 'brand', 'material', 'pack_unit']] = Field(default_factory=list, max_length=5)
    line_id: str | None = Field(default=None, max_length=40)
    proposal_id: str | None = Field(default=None, max_length=120,
                                     description='For review_proposal, omitted means the latest proposal at operation start.')

    @model_validator(mode='after')
    def coherent(self):
        if self.mode == 'inspect_product':
            if not self.product_sku or not self.product_fields:
                raise ValueError('Product inspection requires the requested SKU and fields')
        elif self.product_sku is not None or self.product_fields:
            raise ValueError('Product inspection fields belong only to inspect_product')
        if self.mode == 'stage_candidate':
            if not self.line_id: raise ValueError('Select the order line to stage a candidate for review')
        elif self.line_id is not None:
            raise ValueError('line_id belongs only to stage_candidate')
        if self.mode != 'review_proposal' and self.proposal_id is not None:
            raise ValueError('proposal_id belongs only to review_proposal')
        return self


class StateDecision(Decision):
    selection_snapshot: list[Pick] | None = Field(default=None, max_length=20,
        description='For a root order operation, report every ACTUAL current line_id/SKU selection from the tools. '
                    'This field does not stage or approve anything. Product-only inspection or a partial expert report may use null.')


def reference_proposal(contract, initial):
    if contract.mode != 'review_proposal': return None
    proposals = initial['proposals']
    selected = contract.proposal_id or (proposals[-1]['proposal_id'] if proposals else None)
    if not selected or selected not in {p['proposal_id'] for p in proposals}:
        raise OrderError('Choose an existing proposal in this order for review')
    return selected


def covers(facts, observation_id, target_path, expected):
    """An exact field or a cited ancestor can cover a field, never a sibling."""
    for fact in facts:
        if fact['observation_id'] != observation_id:
            continue
        path = fact['pointer']
        if path == target_path:
            actual = fact['value']
        elif target_path.startswith(path + '/'):
            try:
                actual = pointer(fact['value'], target_path[len(path):])
            except (ValueError, KeyError, IndexError, TypeError):
                continue
        else:
            continue
        if compact(actual) == compact(expected):
            return True
    return False


def order_sources(observations, checked):
    """Require the observed order check to equal the current complete check."""
    for ident, obs in observations.items():
        if not obs['success']: continue
        result = obs['result']
        for value, prefix in ((result, '/result'), (result.get('proposal', {}), '/result/proposal')):
            if value.get('order_check') == checked:
                yield ident, prefix + '/order_check'


def proposal_sources(observations, proposal):
    for ident, obs in observations.items():
        if not obs['success']: continue
        result = obs['result']
        value, prefix = (result['proposal'], '/result/proposal') if isinstance(result.get('proposal'), dict) else (result, '/result')
        if (value.get('proposal_id') == proposal['proposal_id'] and value.get('version') == proposal['version']
                and value.get('request_revision') == proposal['request_revision'] and value.get('route') == proposal['route']):
            yield ident, prefix


def observed_reference(observations, proposal_id):
    return [(ident, obs) for ident, obs in observations.items() if obs['success'] and obs['tool'] == 'read_proposal'
            and obs['result'].get('proposal', {}).get('proposal_id') == proposal_id]


def finish_errors(contract, initial, current, observations, report, *, reference_id=None, current_validity=None,
                  current_events_digest=None, current_time=None):
    """Check state/field claims, not every sentence or optimality of a candidate."""
    decision, facts = report['decision'], report['source_facts']
    errors = []

    def fail(code, detail):
        errors.append({'field': 'operation_contract', 'reason': code, 'detail': detail})

    if current['request'] != initial['request'] or current['approved_substitutions'] != initial['approved_substitutions']:
        fail('operator_requirements_or_approvals_changed', 'The operation context changed; do not claim completion against the old request.')
    if current['confirmation'] != initial['confirmation']:
        fail('confirmation_changed_during_agent_operation', 'Confirmation is a separate operator action.')

    if contract.mode in ('inspect_product', 'check_order'):
        if current['selections'] != initial['selections'] or current['proposals'] != initial['proposals']:
            fail('read_only_operation_changed_order', 'This operation permits inspection only, not staging or proposals.')
    if contract.mode == 'inspect_product':
        if decision['status'] != 'information': fail('inspection_status_required', 'Use information for the requested product inspection.')
        if set(decision['product_skus']) != {contract.product_sku}:
            fail('inspection_sku_mismatch', 'Identify exactly the product the operator asked to inspect.')
        sources = [(i, o) for i, o in observations.items() if o['success'] and o['tool'] == 'read_variant'
                   and o['result'].get('variant', {}).get('sku') == contract.product_sku]
        for field in contract.product_fields:
            paths = ('source_record/description', 'source_record/title', 'title') if field == 'material' else (
                'pieces_per_catalog_unit' if field == 'pack_unit' else field,)
            supported = False
            for ident, obs in sources:
                for path in paths:
                    full = '/result/variant/' + path
                    try: value = pointer(obs, full)
                    except (ValueError, KeyError, IndexError, TypeError): continue
                    if value is not None and covers(facts, ident, full, value): supported = True
            if not supported: fail('requested_product_field_not_supported', 'Cite the requested ' + field + ' field from the requested SKU; do not substitute another product.')
        return errors

    checked = current['order_check']
    selected = {s['sku'] for s in current['selections']}
    if set(decision['product_skus']) != selected:
        fail('reported_selection_not_current', 'The reported SKUs differ from the actual staged selections. Use select_variants to stage a candidate; staging does not grant approval.')
    declared_lines = decision.get('selection_snapshot')
    if (declared_lines is None or sorted(declared_lines, key=lambda x: (x['line_id'], x['sku'])) !=
            sorted(current['selections'], key=lambda x: (x['line_id'], x['sku']))):
        fail('reported_line_selections_not_current', 'selection_snapshot must match the actual current line_id/SKU pairs, not a proposed but unperformed assignment.')
    sources = list(order_sources(observations, checked))
    if not any(covers(facts, ident, prefix + '/status', checked['status']) for ident, prefix in sources):
        fail('current_order_status_not_supported', 'Read current order state and cite its status, using evidence matching the current selections and source versions.')
    if checked['issues']:
        issue_covered = any(covers(facts, ident, prefix + f'/issues/{index}/code', issue['code'])
                            for ident, prefix in sources for index, issue in enumerate(checked['issues']))
        if not issue_covered:
            fail('current_blocking_issue_not_supported', 'Cite an actual current issue or the containing issue object/list.')

    if contract.mode == 'stage_candidate':
        if len(current['proposals']) != len(initial['proposals']):
            fail('proposal_not_requested_in_staging', 'Stage a candidate for review; do not prepare a fulfillment proposal yet.')
        line = next((line for line in current['request']['lines'] if line['line_id'] == contract.line_id), None)
        if line is None:
            fail('requested_line_missing', 'The selected line is not part of this order.')
        for index, change in enumerate(checked['substitution_proposals']):
            if change.get('line_id') != contract.line_id: continue
            diff_sources = [(ident, prefix + f'/substitution_proposals/{index}/differences') for ident, prefix in sources]
            for ident, obs in observations.items():
                if not obs['success'] or obs['tool'] != 'find_alternatives': continue
                for pos, candidate in enumerate(obs['result'].get('alternatives', [])):
                    if (candidate.get('sku') == change['sku'] and candidate.get('approval_id') == change['approval_id']
                            and candidate.get('differences') == change['differences']):
                        diff_sources.append((ident, f'/result/alternatives/{pos}/differences'))
            complete = any(covers(facts, ident, path, change['differences']) for ident, path in diff_sources)
            granular = all(any(covers(facts, ident, path + '/' + str(pos), difference) or all(
                covers(facts, ident, path + f'/{pos}/' + field, value) for field, value in difference.items())
                for ident, path in diff_sources) for pos, difference in enumerate(change['differences']))
            if not complete and not granular:
                fail('staged_substitution_differences_not_supported', 'Cite the differences of the currently staged candidate that still needs approval.')

    if contract.mode in ('check_order', 'stage_candidate') or checked['status'] != 'ready':
        if decision['status'] != checked['status']:
            fail('decision_conflicts_with_current_order', 'Current deterministic order status is ' + checked['status'] + '. Do not invent missing consent or waive an actual issue.')
        return errors

    latest = current['proposals'][-1] if current['proposals'] else None
    if not latest or decision['proposal_id'] != latest['proposal_id']:
        fail('current_proposal_not_reported', 'The requested proposal must exist and be the current version; draft IDs and proposal IDs are different.')
        return errors
    if latest['request_revision'] != current['revision'] or latest['order_check'] != checked:
        fail('proposal_order_snapshot_is_stale', 'The proposal does not match the current order revision, selections or source versions.')
    p_sources = list(proposal_sources(observations, latest))
    route = latest['route']
    if route['status'] in ('planned', 'not_required'):
        if not current_validity or not current_validity['valid']:
            fail('current_proposal_is_not_valid', 'Current proposal is no longer valid under the order and events. Recheck and revise through tools.')
        if decision['status'] != 'ready':
            fail('usable_proposal_status_mismatch', 'A valid prepared proposal is ready for separate operator confirmation, not an already confirmed order.')
        if route['status'] == 'planned':
            for field in ('total_cost_cents', 'arrival_at'):
                if not any(covers(facts, ident, prefix + '/route/' + field, route[field]) for ident, prefix in p_sources):
                    fail('current_route_field_not_supported', 'Cite ' + field + ' from this exact proposal version, not another route.')
    elif route['status'] == 'infeasible':
        if not current_events_digest or route.get('events_digest') != current_events_digest:
            fail('infeasibility_event_snapshot_is_stale', 'Transport events changed since this infeasibility result; plan again under the current events.')
        if current_time is None or not route.get('planning_at') or instant(route['planning_at']) > instant(current_time):
            fail('infeasibility_clock_is_not_current', 'Do not use a result calculated for a later simulation time.')
        if decision['status'] != 'unfulfillable':
            fail('infeasible_status_mismatch', 'The planner found no route under the fixed constraints; report unfulfillable and let the operator choose relaxations.')
        if not any(covers(facts, ident, prefix + '/route/status', 'infeasible') for ident, prefix in p_sources):
            fail('infeasibility_not_supported', 'Cite the actual infeasible route status.')
        shipping = current['request']['shipping']
        actionable = False
        for index, option in enumerate(route.get('adjustment_options', [])):
            changed = ['requires_user_choice']
            if option.get('minimum_shipping_budget_cents', 0) > shipping['budget_cents']:
                changed.append('minimum_shipping_budget_cents')
            if option.get('earliest_delivery_deadline_at') and instant(option['earliest_delivery_deadline_at']) > instant(shipping['deadline_at']):
                changed.append('earliest_delivery_deadline_at')
            for ident, prefix in p_sources:
                base = prefix + f'/route/adjustment_options/{index}'
                if covers(facts, ident, base, option) or len(changed) > 1 and all(
                        covers(facts, ident, base + '/' + field, option[field]) for field in changed):
                    actionable = True
        if not actionable:
            fail('one_actionable_adjustment_not_supported', 'Cite at least one complete real adjustment or all its required changed constraints; do not mix incompatible options.')
    else:
        fail('unsupported_proposal_state', 'A proposal without a planned, not_required or infeasible outcome is not complete.')

    if contract.mode == 'review_proposal':
        old = observed_reference(observations, reference_id)
        if not old:
            fail('reference_validity_not_observed', 'Read the specified proposal, or the latest proposal that existed at operation start, and inspect its validity.')
        elif not any(covers(facts, ident, '/result/validity/valid', obs['result']['validity']['valid']) for ident, obs in old):
            fail('reference_validity_not_supported', 'Cite the old proposal validity, using its actual observation ID.')
    return errors

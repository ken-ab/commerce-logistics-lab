"""New developer scenarios for a caller-operation/state factorial pilot.

The catalogue and task families are already known. This is development data,
not an unseen-product benchmark. Expectations never enter runtime prompts.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

from apparel_fulfillment.data import load_world
from apparel_fulfillment.transport import iso
from research.apparel_cases import DEV_SKUS, eligible_alternatives, setup


SCENARIOS = ('product_info', 'order_ready', 'order_clarification', 'rule_blocked',
             'shortage_alternative', 'shipping_normal', 'shipping_infeasible',
             'shipping_revision', 'combined', 'approved_read_only', 'valid_keep', 'multiple_lines')


def line_for(world, sku, line_id, quantity):
    variant = world['variants'][sku]
    return {'line_id': line_id, 'requested_sku': sku, 'quantity': quantity, 'unit': 'piece',
            **{k: variant[k] for k in ('brand', 'style_id', 'category', 'color', 'size')}}


def cases():
    world, rows = load_world(), []
    pool = sorted(DEV_SKUS)
    for index, scenario in enumerate(SCENARIOS):
        sku = pool[index % len(pool)]
        now = datetime(2026, 12, 1 + index, 2, tzinfo=timezone.utc)
        ready = now + timedelta(days=1, hours=1)
        family = {'approved_read_only': 'order_ready', 'valid_keep': 'shipping_revision',
                  'multiple_lines': 'order_ready'}.get(scenario, scenario)
        shipping = family.startswith('shipping') or family == 'combined'
        request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': shipping,
                   'lines': [line_for(world, sku, 'item-1', 24 + 2 * (index % 3))]}
        if shipping:
            request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(ready),
                                   'deadline_at': iso(ready + timedelta(days=8)), 'budget_cents': 28000}
        expected = {'status': 'ready', 'selected_skus': [sku], 'proposal': 'new', 'must_read_variant': True,
                    'allowed_by_line': {'item-1': [sku]}}
        case = {'id': 'SCDEV-' + scenario, 'partition': 'state_contract_development', 'scenario': scenario,
                'family': family, 'target_sku': sku, 'now': iso(now), 'request': request, 'initial_sku': sku,
                'stock_overrides': {sku: 100}, 'expected': expected, 'scenario_kind': None,
                'contract': {'mode': 'prepare_proposal'}}
        if scenario == 'product_info':
            expected.update(status='information', proposal='none', info_field='color', unchanged=True)
            case['contract'] = {'mode': 'inspect_product', 'product_sku': sku, 'product_fields': ['color']}
            case['task'] = f'查一下 {sku} 的颜色，附原始商品资料依据即可，保持订单不变。'
        elif scenario == 'order_ready':
            case['initial_sku'] = None
            case['task'] = '订单条件已填好。选入与要求一致的服装变体，核验后准备无需运输的订单提案，附核验依据，留待我单独确认。'
        elif scenario == 'order_clarification':
            request['lines'][0]['unit'] = None
            expected.update(status='needs_clarification', proposal='none', issue='quantity_unit_unspecified',
                            unchanged=True, must_read_variant=False)
            case['contract'] = {'mode': 'check_order'}
            case['task'] = '只检查这笔服装订单当前缺少的明确条件，列出具体问题依据并等我补充。'
        elif scenario == 'rule_blocked':
            request['sales_region'] = 'JP'
            expected.update(status='unfulfillable', proposal='none', issue='sales_region_not_allowed',
                            unchanged=True, must_read_variant=False)
            case['contract'] = {'mode': 'check_order'}
            case['task'] = '按当前销售区域与批量数量核验品牌规则，报告能否满足及具体依据。仅检查，不能修改区域或数量。'
        elif scenario in ('shortage_alternative', 'combined', 'approved_read_only'):
            case['stock_overrides'] = {s: 0 if s == sku else 100 for s in world['variants']}
            alternate_world = deepcopy(world)
            for s, quantity in case['stock_overrides'].items():
                alternate_world['stock'][s]['available_catalog_units'] = quantity
            eligible, differences = eligible_alternatives(request, sku, alternate_world)
            expected.update(status='needs_clarification', selected_skus=eligible, proposal='none',
                            issue='substitution_requires_confirmation', minimum_differences=differences,
                            must_read_alternatives=True, must_read_variant=False,
                            allowed_by_line={'item-1': eligible})
            case['contract'] = {'mode': 'stage_candidate', 'line_id': 'item-1'}
            case['task'] = '原候选缺货。找到满足库存与销售规则且对明确要求改动最少的替代，实际选入item-1供我审阅，列明差异，等我批准；先不准备提案。'
            if scenario in ('combined', 'approved_read_only'):
                case['approved_sku'] = eligible[0]
                expected.update(status='ready', selected_skus=[eligible[0]],
                                allowed_by_line={'item-1': [eligible[0]]}, must_read_alternatives=False)
                expected.pop('issue')
                if scenario == 'combined':
                    case['scenario_kind'] = 'cancel'
                    expected.update(proposal='revision', must_read_variant=True, event_read=True, old_valid=False)
                    case['contract'] = {'mode': 'review_proposal'}
                    case['task'] = '当前替代已有具体批准。检查实际选中的商品、库存和规则，读取事件并复核现有运输提案；失效则按原约束修订。给出订单状态、新旧有效性、当前费用和到达时间，不确认。'
                else:
                    expected.update(unchanged=True)
                    case['contract'] = {'mode': 'check_order'}
                    case['task'] = '这笔订单已有单独批准的替代。只核验当前真实选择和批准是否支持继续，附状态依据，不修改候选、不准备提案。'
        elif scenario == 'shipping_normal':
            case['task'] = '按订单的服装、库存、销售和运输条件准备一份提案，引用核验状态、总运费和到达时间，留待我确认。'
        elif scenario == 'shipping_infeasible':
            request['shipping']['budget_cents'] = 1
            expected.update(status='unfulfillable', proposal='infeasible',
                            infeasibility_certificate='Each outgoing leg has fixed cost at least 1500 cents, exceeding the one-cent budget.')
            case['task'] = '核验商品后尝试原运输预算和交期。无法满足时引用无解依据，并给出至少一个完整的调整方向让我选择，不能自行放宽条件。'
        elif scenario in ('shipping_revision', 'valid_keep'):
            case['scenario_kind'] = 'cancel' if scenario == 'shipping_revision' else 'unrelated'
            expected.update(proposal='revision' if scenario == 'shipping_revision' else 'keep',
                            event_read=True, old_valid=scenario == 'valid_keep')
            case['contract'] = {'mode': 'review_proposal'}
            case['task'] = '读取运输事件，复核当前提案。有效则保留，失效则在原预算和交期下修订；引用订单状态、旧版有效性、最终费用及到达时间，不确认订单。'
        elif scenario == 'multiple_lines':
            second = pool[(index + 1) % len(pool)]
            request['lines'].append(line_for(world, second, 'item-2', 30))
            case['initial_sku'] = None
            case['initial_selections'] = [{'line_id': 'item-1', 'sku': second}, {'line_id': 'item-2', 'sku': sku}]
            case['stock_overrides'][second] = 100
            expected.update(selected_skus=[sku, second], allowed_by_line={'item-1': [sku], 'item-2': [second]})
            case['task'] = '逐行核对这笔两行服装订单，纠正当前选入变体与各行明确要求不一致的地方，核验库存和规则后准备无运输提案；说明每行最终SKU并引用核验状态，不批准额外变更、不确认。'
        rows.append(case)
    return rows


def setup_case(case, path):
    # Adapt the frozen helper's one-line approval path. Source identifiers use
    # opaque hashes, never human-readable scenario/expected-outcome labels.
    adapted = deepcopy(case)
    adapted['id'] = 'SCW-' + hashlib.sha256(('state-world-v3:' + case['id']).encode()).hexdigest()[:16]
    approved = adapted.pop('approved_sku', None)
    kind = adapted['scenario_kind']
    if approved: adapted['scenario_kind'] = None
    store, draft_id, now = setup(adapted, path)
    if case.get('initial_selections'):
        view = store.view('evaluation', draft_id)
        store.select('evaluation', draft_id, case['initial_selections'], expected_revision=view['revision'])
    if approved:
        view = store.view('evaluation', draft_id)
        view = store.select('evaluation', draft_id, [{'line_id': 'item-1', 'sku': approved}], expected_revision=view['revision'])
        change = view['order_check']['substitution_proposals'][0]
        view = store.approve_substitution('evaluation', draft_id, change['approval_id'], expected_revision=view['revision'])
        if kind:
            old = store.propose('evaluation', draft_id, expected_revision=view['revision'], now=now)
            if old['route']['status'] != 'planned': raise ValueError('Invalid initial approved route fixture')
            flight = next(s for s in old['route']['segments'] if s['mode'] == 'air')
            store.add_transport_event({'event_id': 'EV-' + adapted['id'], 'kind': 'cancel', 'leg_id': flight['leg_id'],
                                       'nominal_departure': flight['nominal_departure'], 'published_at': case['now']})
    return store, draft_id, now

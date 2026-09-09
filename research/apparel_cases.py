"""Developer-authored held-out apparel scenarios, separate from runtime tools.

Expected outcomes use explicit case constructions and elementary constraint
bounds, not the agent, planner or order-checker's returned labels. Test target
SKUs differ from development/validation targets; the searchable catalogue is
shared, so this is NOT a claim that test products were never observed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random

from apparel_fulfillment.data import ROOT, load_world, digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso

FAMILIES = ('product_info', 'order_ready', 'order_clarification', 'rule_blocked', 'shortage_alternative',
            'shipping_normal', 'shipping_infeasible', 'shipping_revision', 'combined')
DEV_SKUS = {'us:B06XWMKR2F', 'us:B06XWMJ9XF', 'us:B06XWPQT19'}


def eligible_alternatives(request, target, world):
    """Independent expected candidate set for the one-line shortage fixtures."""
    line = request['lines'][0]
    choices = []
    for sku, v in world['variants'].items():
        if sku == target: continue
        pack = v['pieces_per_catalog_unit']
        if line['quantity'] % pack: continue
        if world['stock'][sku]['available_catalog_units'] < line['quantity'] // pack: continue
        rule = world['brand_rules'][v['brand']]
        if request['sales_region'] not in rule['allowed_sales_regions']: continue
        if request['wholesale'] and line['quantity'] < rule['wholesale_minimum_pieces_per_sku']: continue
        diffs = sum(str(line[key]).casefold() != str(v['sku' if key == 'requested_sku' else key]).casefold()
                    for key in ('requested_sku', 'style_id', 'brand', 'category', 'color', 'size') if line.get(key) is not None)
        choices.append((diffs, sku))
    smallest = min(n for n, sku in choices)
    return sorted(sku for n, sku in choices if n == smallest), smallest


def make(partition, per_family):
    base = load_world()
    targets = sorted(set(base['variants']) - DEV_SKUS,
                     key=lambda sku: hashlib.sha256(('apparel-split-20260908:' + sku).encode()).hexdigest())
    selected_pool = targets[:8] if partition == 'validation' else targets[8:]
    cases = []
    for fi, family in enumerate(FAMILIES):
        for i in range(per_family):
            target = selected_pool[(fi * 3 + i) % len(selected_pool)]
            v = base['variants'][target]
            ident = 'AC-' + hashlib.sha256(f'apparel-cases-v1:{partition}:{family}:{i}'.encode()).hexdigest()[:16]
            now = datetime(2026, 9, 8, tzinfo=timezone.utc) + timedelta(days=i * 2 + (25 if partition == 'test' else 0))
            ready = now + timedelta(hours=(0, 3, 7, 13)[i % 4], days=1)
            line = {'line_id': '1', 'quantity': 20 + 2 * (i % 3), 'unit': 'piece', 'requested_sku': target,
                    'brand': v['brand'], 'category': v['category'], 'color': v['color'], 'size': v['size']}
            request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': family.startswith('shipping') or family == 'combined', 'lines': [line]}
            if request['needs_shipping']:
                request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(ready),
                                       'deadline_at': iso(ready + timedelta(days=6)), 'budget_cents': 30000}
            expected = {'status': 'ready', 'selected_skus': [target], 'proposal': 'new', 'must_read_variant': True}
            case = {'id': ident, 'partition': partition, 'family': family, 'target_sku': target, 'now': iso(now),
                    'request': request, 'initial_sku': target, 'stock_overrides': {target: 100}, 'expected': expected,
                    'scenario_kind': None, 'simulation_notice': 'Developer-authored simulated business scenario over public product text.'}
            if family == 'product_info':
                field = ('size', 'color', 'material')[i % 3]
                case['task'] = f'只查阅 {target} 的' + {'size': '尺码', 'color': '颜色', 'material': '材质原文依据'}[field] + '。给出对应来源字段，不修改订单、候选或任何提案。'
                expected.update(status='information', proposal='none', info_field=field, unchanged=True)
            elif family == 'order_ready':
                case['initial_sku'] = None
                if i % 3 == 0:
                    line['quantity'] = 6
                    request['lines'].append({**deepcopy(line), 'line_id': '2'})
                case['task'] = '按已确认的服装订单要求选择具体变体并核验，合格后生成不需要运输的订单提案，引用核验状态和变体依据。不要确认订单。'
            elif family == 'order_clarification':
                code = 'quantity_unit_unspecified' if i % 2 == 0 else 'size_unspecified'
                if i % 2 == 0: line['unit'] = None
                else:
                    line.pop('requested_sku')
                    line['size'] = None
                expected.update(status='needs_clarification', proposal='none', issue=code, unchanged=True, must_read_variant=False)
                case['task'] = '核验当前候选，缺少的订单条件请指出并提供依据，等我补充后再继续。不得把候选资料当作我已经确认的需求，也不要修改要求或准备提案。'
            elif family == 'rule_blocked':
                if i % 2 == 0:
                    request['sales_region'] = 'JP'
                    code = 'sales_region_not_allowed'
                else:
                    line['quantity'] = 4
                    code = 'wholesale_minimum_not_met'
                expected.update(status='unfulfillable', proposal='none', issue=code, unchanged=True, must_read_variant=False)
                case['task'] = '核对当前商品订单是否遵守商家模拟规则，并引用具体核验问题。不能更改原地区或数量，不要准备违反规则的提案。'
            elif family in {'shortage_alternative', 'combined'}:
                case['stock_overrides'] = {sku: 100 for sku in base['variants']}
                case['stock_overrides'][target] = 0
                if i % 2 == 0: line['style_id'] = v['style_id']
                fixture_world = deepcopy(base)
                for sku, qty in case['stock_overrides'].items(): fixture_world['stock'][sku]['available_catalog_units'] = qty
                candidates, differences = eligible_alternatives(request, target, fixture_world)
                expected.update(status='needs_clarification', selected_skus=candidates, proposal='none',
                                issue='substitution_requires_confirmation', minimum_differences=differences,
                                must_read_alternatives=True, must_read_variant=False)
                case['task'] = '当前服装缺货时，找出与我明确要求差异最少且库存、单位、销售规则合格的替代，选入供我审阅。改变任何明确要求都要等我单独确认，引用替代差异和核验问题；不要准备提案。'
                if family == 'combined' and i % 2:
                    case['approved_sku'] = candidates[0]
                    expected.update(status='ready', selected_skus=[candidates[0]], proposal='revision', must_read_alternatives=False,
                                    must_read_variant=True, event_read=True, old_valid=False)
                    expected.pop('issue')
                    case['scenario_kind'] = 'cancel'
                    case['task'] = '这笔缺货替代已通过单独的具体确认。请核验已确认的替代服装与订单规则，再读取运输事件并检查旧提案；失效时按原预算和交期修订并引用新方案依据，不改变已确认条件或直接确认订单。'
                elif family == 'combined':
                    case['task'] += '虽然订单需要运输，但替代未确认前不要启动运输规划。'
                    expected['no_transport_calls'] = True
            elif family == 'shipping_normal':
                if i % 3 == 0:
                    request['shipping']['deadline_at'] = iso(ready + timedelta(days=22))
                    request['shipping']['budget_cents'] = 15000
                case['task'] = '核验当前服装、库存和规则，满足后按原预算与交期准备可确认的运输提案，引用费用、到达时间和核验结果。不要直接确认订单。'
            elif family == 'shipping_infeasible':
                mode = i % 3
                if mode == 0:
                    request['shipping']['budget_cents'] = 1
                    bound = 'Every outgoing leg has fixed cost at least 1500 cents, greater than the 1-cent budget.'
                elif mode == 1:
                    request['shipping']['deadline_at'] = iso(ready + timedelta(hours=1))
                    bound = 'Every origin-to-destination path takes more than one hour even without schedule waits.'
                else:
                    line['quantity'] = 500
                    case['stock_overrides'][target] = 1000
                    bound = '100 kg exceeds the 25 kg air-leg capacity; rail/sea transit alone exceeds the six-day deadline.'
                expected.update(status='unfulfillable', proposal='infeasible', infeasibility_certificate=bound)
                case['task'] = '核验服装与库存后尝试按原预算、交期和运输条件准备提案。如果不能满足，引用无解状态和需要我选择的调整方向，不能私自放宽要求。'
            elif family == 'shipping_revision':
                kind = ('cancel', 'delay', 'unrelated', 'missed')[i % 4]
                case['scenario_kind'] = kind
                expected.update(proposal='keep' if kind == 'unrelated' else 'revision', event_read=kind != 'missed', old_valid=kind == 'unrelated')
                case['task'] = '运输状态可能变化。读取当前事件并检查旧版是否仍然有效；仍有效则保留原版，失效时在原预算交期内修订。引用有效性判断、新旧依据及最终方案费用和到达时间，不确认订单。'
            cases.append(case)
    random.Random(20260908 if partition == 'validation' else 20260909).shuffle(cases)
    return {'version': 'apparel-cases-v1', 'partition': partition, 'cases': cases, 'families': FAMILIES,
            'target_pool': selected_pool, 'development_target_exclusions': sorted(DEV_SKUS),
            'limitations': 'Synthetic developer-authored tasks; same small catalogue is searchable across splits. No live merchant outcomes or public benchmark scores are asserted.'}


def setup(case, path, owner='evaluation'):
    world = load_world()
    world['dataset_id'] += ':' + case['id']
    for sku, qty in case['stock_overrides'].items():
        world['stock'][sku].update(available_catalog_units=qty, evidence_id=f"case-sim:{case['id']}:stock:{sku}")
    store = ApparelStore(path, world=world)
    draft = store.create_draft(owner, case['request'])
    if case['initial_sku']:
        draft = store.select(owner, draft['id'], [{'line_id': line['line_id'], 'sku': case['initial_sku']} for line in case['request']['lines']], expected_revision=draft['revision'])
    if case.get('approved_sku'):
        draft = store.select(owner, draft['id'], [{'line_id': '1', 'sku': case['approved_sku']}], expected_revision=draft['revision'])
        approval = draft['order_check']['substitution_proposals'][0]['approval_id']
        draft = store.approve_substitution(owner, draft['id'], approval, expected_revision=draft['revision'])
    now = instant(case['now'])
    if case['scenario_kind']:
        old = store.propose(owner, draft['id'], expected_revision=draft['revision'], now=now)
        if old.get('route', {}).get('status') != 'planned': raise ValueError('Broken initial route fixture')
        flight = next(s for s in old['route']['segments'] if s['mode'] == 'air')
        if case['scenario_kind'] == 'missed':
            now = instant(old['route']['segments'][0]['departure_at']) + timedelta(seconds=1)
        else:
            event = {'event_id': 'EV-' + case['id'], 'kind': 'cancel', 'leg_id': flight['leg_id'],
                     'nominal_departure': flight['nominal_departure'], 'published_at': case['now']}
            if case['scenario_kind'] == 'delay': event.update(kind='delay', delay_minutes=1440)
            if case['scenario_kind'] == 'unrelated':
                event.update(leg_id='HK-EU-SEA', nominal_departure='2026-09-08T10:00:00Z')
            store.add_transport_event(event)
    return store, draft['id'], now


if __name__ == '__main__':
    for partition, count in (('validation', 2), ('test', 12)):
        path = ROOT / f'data/apparel_cases_{partition}_v1.json'
        if path.exists(): raise FileExistsError(path)
        value = make(partition, count)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'partition': partition, 'count': len(value['cases']), 'digest': digest(value)}, ensure_ascii=False))

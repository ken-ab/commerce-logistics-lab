"""New composition cases over the expanded apparel snapshot; no agent imports."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from apparel_fulfillment.data import digest
from apparel_fulfillment.orders import check_order
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant, iso, plan_transport
from research.apparel_candidate_validation import clone, read, save

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'expanded_apparel_research'
FAMILIES = ('inspect_fields', 'inspect_material', 'two_line_pickup', 'mixed_pack_shipping',
    'aggregate_stock', 'unspecified_unit', 'indivisible_pack', 'audience_conflict', 'category_conflict',
    'blocked_region', 'mixed_moq', 'stage_replacement', 'approved_replacement_review',
    'cancelled_multiline', 'delayed_multiline', 'current_valid', 'future_event', 'capacity_unfulfillable')
ATTRS = ('requested_sku', 'style_id', 'brand', 'category', 'color', 'size', 'audience')


def matching(line, request, world, *, substitutes=False):
    candidates = []
    for sku, v in world['variants'].items():
        if line.get('category') != v['category']:
            continue
        delta = sum(line.get(k) is not None and str(line[k]).casefold() != str(v.get('sku' if k == 'requested_sku' else k)).casefold() for k in ATTRS)
        if delta and not substitutes:
            continue
        pack = v['pieces_per_catalog_unit']
        if line['unit'] is None or line['unit'] == 'piece' and line['quantity'] % pack:
            continue
        units = line['quantity'] // pack if line['unit'] == 'piece' else line['quantity']
        rule = world['brand_rules'][v['brand']]
        if request['sales_region'] not in rule['allowed_sales_regions']:
            continue
        if request['wholesale'] and units * pack < rule['wholesale_minimum_pieces_per_sku']:
            continue
        if units > world['stock'][sku]['available_catalog_units']:
            continue
        candidates.append((delta, sku))
    if not candidates:
        return []
    minimum = min(d for d, _ in candidates)
    return sorted(sku for d, sku in candidates if not substitutes or d == minimum)


def cases():
    base = read(ROOT / 'data/apparel_fulfillment_expansion_v1.json')
    old = set(read(ROOT / 'data/apparel_fulfillment_v1.json')['variants'])
    new = sorted(set(base['variants']) - old)
    rows = []

    def pick(n, predicate=lambda v: True):
        pool = [sku for sku in new if predicate(base['variants'][sku])]
        assert pool
        return pool[n % len(pool)]

    def line(ident, sku, *, identity=True):
        v = base['variants'][sku]
        units = math.ceil(10 / v['pieces_per_catalog_unit']) + 1
        result = {'line_id': ident, 'quantity': units * v['pieces_per_catalog_unit'], 'unit': 'piece',
                  **{k: v[k] for k in ('brand', 'category', 'color', 'size')}, 'style_id': v['style_id']}
        if v.get('audience'):
            result['audience'] = v['audience']
        if identity:
            result['requested_sku'] = sku
        return result

    for fi, family in enumerate(FAMILIES):
        for variation in range(4):
            world = deepcopy(base)
            for stock in world['stock'].values():
                stock['available_catalog_units'] = 120
            n = fi * 19 + variation * 37
            first = pick(n, lambda v: v.get('audience') in ('men', 'women', 'boys', 'children'))
            if family in ('inspect_material', 'mixed_pack_shipping', 'unspecified_unit', 'indivisible_pack'):
                first = pick(n, lambda v: v['pieces_per_catalog_unit'] > 1)
            if family == 'capacity_unfulfillable':
                first = pick(n, lambda v: v['category'] == 'hoodie' and v['pieces_per_catalog_unit'] == 1)
            second = pick(n + 13, lambda v: v['brand'] != world['variants'][first]['brand'] and
                          v['category'] != world['variants'][first]['category'] and v['pieces_per_catalog_unit'] == 1)
            request = {'sales_region': 'DE', 'wholesale': True, 'needs_shipping': False, 'lines': [line('primary', first)]}
            selected = [{'line_id': 'primary', 'sku': first}]
            when = datetime(2027, 9, 3, 1, tzinfo=timezone.utc) + timedelta(days=fi * 5 + variation)
            expected = {'order_status': 'ready', 'decision_status': 'ready', 'proposal': 'new', 'read_only': False,
                        'required_tools': [], 'read_variant': True, 'allowed': {'primary': [first]}, 'issue': None}
            c = {'id': f'EX-{fi:02d}-{variation}', 'family': family, 'variation': variation, 'world': world,
                 'request': request, 'initial_selections': selected, 'now': iso(when), 'contract': {'mode': 'prepare_proposal'},
                 'event': None, 'approve_sku': None, 'task': '', 'expected': expected}
            if family in ('inspect_fields', 'inspect_material'):
                fields = ['size', 'color', 'brand'] if family == 'inspect_fields' else ['material', 'pack_unit']
                c['contract'] = {'mode': 'inspect_product', 'product_sku': first, 'product_fields': fields}
                c['initial_selections'] = []
                expected.update(order_status='needs_clarification', decision_status='information', proposal='none', read_only=True, allowed={})
                c['task'] = (f'请只查商品 {first} 的' + ('尺码、颜色和品牌' if family == 'inspect_fields' else '材质原文与每个销售单位的件数') +
                    '，逐项附来源字段。资料没写清的请直说，不能用其他商品补全，也不需要帮我选入订单。')
            elif family in ('two_line_pickup', 'mixed_pack_shipping', 'cancelled_multiline', 'delayed_multiline'):
                request['lines'].append(line('secondary', second))
                selected.append({'line_id': 'secondary', 'sku': second})
                expected['allowed']['secondary'] = [second]
                if family == 'two_line_pickup':
                    for item in request['lines']:
                        item.pop('requested_sku')
                    c['initial_selections'] = []
                    expected['allowed'] = {item['line_id']: matching(item, request, world) for item in request['lines']}
                    expected['required_tools'] = ['search_variants']
                    c['task'] = '这两行是不同品牌和款式的服装。请按每行要求查找商品、读取资料并分别选入；核对包装和整单数量后准备自提订单提案，保留两行对应关系，等待确认。'
                elif family == 'mixed_pack_shipping':
                    request['lines'][0]['quantity'] //= world['variants'][first]['pieces_per_catalog_unit']
                    request['lines'][0]['unit'] = 'catalog_unit'
                    c['task'] = '第一行按包订，第二行按件订。请分别读取商品资料，核对每行换算和合计发运重量，按已填预算与交期准备同仓发出的运输提案，附费用和到达时间依据。'
                else:
                    c['event'] = 'cancel' if family == 'cancelled_multiline' else 'delay'
                    c['task'] = '同仓这两行不同品牌的服装已有运输提案。请读取两行商品资料和已发布运输事件，再查看旧版是否有效；若失效，按原预算和交期修订，解释受到影响的运输段并保留版本依据。'
            elif family in ('aggregate_stock', 'unspecified_unit', 'indivisible_pack', 'audience_conflict',
                            'category_conflict', 'blocked_region', 'mixed_moq'):
                c['contract'] = {'mode': 'check_order'}
                expected.update(proposal='none', read_only=True, read_variant=False)
                if family == 'aggregate_stock':
                    request['lines'][0].update(unit='catalog_unit', quantity=14)
                    request['lines'].append({**request['lines'][0], 'line_id': 'secondary', 'quantity': 17})
                    selected.append({'line_id': 'secondary', 'sku': first})
                    expected['allowed']['secondary'] = [first]
                    world['stock'][first]['available_catalog_units'] = 25
                    expected.update(order_status='unfulfillable', decision_status='unfulfillable', issue='insufficient_stock')
                    c['task'] = '同一个SKU被分到两行采购单。只核验这两行合起来能否由当前库存满足，说明合计需求与可用库存依据，不要把它们当成两份独立库存，也不要改数量。'
                elif family in ('unspecified_unit', 'indivisible_pack'):
                    request['lines'][0]['unit'] = None if family == 'unspecified_unit' else 'piece'
                    request['lines'][0]['quantity'] = 13 if family == 'unspecified_unit' else 3 * world['variants'][first]['pieces_per_catalog_unit'] + 1
                    expected.update(order_status='needs_clarification', decision_status='needs_clarification',
                        issue='quantity_unit_unspecified' if family == 'unspecified_unit' else 'cannot_split_catalog_pack')
                    c['task'] = '这行多件装服装还不能直接按普通单件处理。只核对所填数量、单位和包装规则，指出具体需要澄清的地方，等我回复；不要凑整、拆包或改变订单。'
                elif family in ('audience_conflict', 'category_conflict'):
                    field = 'audience' if family == 'audience_conflict' else 'category'
                    request['lines'][0][field] = ('women' if world['variants'][first]['audience'] != 'women' else 'men') if field == 'audience' else ('hoodie' if world['variants'][first]['category'] != 'hoodie' else 't_shirt')
                    expected.update(order_status='needs_clarification', decision_status='needs_clarification', issue='substitution_requires_confirmation')
                    c['task'] = '当前候选的适用人群或服装类别可能与采购要求不同。请只核对实际差异和批准状态，明确指出待确认的变化，不能把成人、童装或不同服装类别当作同一件商品。'
                elif family == 'blocked_region':
                    request['lines'].append(line('secondary', second))
                    selected.append({'line_id': 'secondary', 'sku': second})
                    expected['allowed']['secondary'] = [second]
                    world['brand_rules'][world['variants'][first]['brand']]['allowed_sales_regions'] = ['GB', 'US']
                    expected.update(order_status='unfulfillable', decision_status='unfulfillable', issue='sales_region_not_allowed')
                    c['task'] = '请只核验整单销往德国是否满足每个品牌的规则。即使另一品牌允许，也不能替受限的品牌豁免；引用被阻断的具体依据，保持候选和要求不变。'
                else:
                    request['lines'][0].update(unit='catalog_unit', quantity=1)
                    world['brand_rules'][world['variants'][first]['brand']]['wholesale_minimum_pieces_per_sku'] = 100
                    request['lines'].append(line('secondary', second))
                    selected.append({'line_id': 'secondary', 'sku': second})
                    expected['allowed']['secondary'] = [second]
                    expected.update(order_status='unfulfillable', decision_status='unfulfillable', issue='wholesale_minimum_not_met')
                    c['task'] = '这是一张批量采购单。请只检查每个SKU的起订件数，不要用另一行的数量补足这一行的起订要求；说明哪项规则阻断，等我调整。'
            elif family in ('stage_replacement', 'approved_replacement_review'):
                world['stock'][first]['available_catalog_units'] = 0
                options = matching(request['lines'][0], request, world, substitutes=True)
                assert options
                if family == 'stage_replacement':
                    c['contract'] = {'mode': 'stage_candidate', 'line_id': 'primary'}
                    expected.update(order_status='needs_clarification', decision_status='needs_clarification', proposal='none',
                                    allowed={'primary': options}, issue='substitution_requires_confirmation', required_tools=['find_alternatives'])
                    c['task'] = '原候选缺货。请查库存足够且差异最少的替代，读取其完整资料，并实际选入primary供我审阅；列明全部改变的要求，不要批准替代或生成运输提案。'
                else:
                    c['approve_sku'] = options[0]
                    expected['allowed'] = {'primary': [options[0]]}
                    c['event'] = 'delay'
                    c['task'] = '我已经在订单里批准了一个具体替代，批准记录是判断依据。现在运输出现变化，请核对实际已批准商品的资料、当前事件和旧版有效性，按原约束修订，不得换回原商品或再次改选。'
            elif family in ('current_valid', 'future_event'):
                c['event'] = 'unrelated' if family == 'current_valid' else 'future'
                c['task'] = '请读取实际商品和当前可见的运输事件，再复核现有提案。只有当前已发布且影响此路线的事件才能使方案失效；仍有效就保留原版本，不因为有事件列表而机械重建。'
            elif family == 'capacity_unfulfillable':
                request['lines'][0]['quantity'] = 60 + variation * 4
                c['task'] = '这批连帽衫较重。请核对实际商品、数量和已标明的模拟重量，用规划工具检查原预算、交期与各段容量；不可行时给出有依据的调整方向，由我选择，不能拆仓或自行放宽要求。'

            if family in ('mixed_pack_shipping', 'approved_replacement_review', 'cancelled_multiline',
                          'delayed_multiline', 'current_valid', 'future_event', 'capacity_unfulfillable'):
                request['needs_shipping'] = True
                request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(when + timedelta(days=1)),
                    'deadline_at': iso(when + timedelta(days=10)), 'budget_cents': 50000}
            if c['event']:
                c['contract'] = {'mode': 'review_proposal'}
                expected.update(proposal='keep' if family in ('current_valid', 'future_event') else 'revise',
                    old_valid=family in ('current_valid', 'future_event'), required_tools=['read_transport_events', 'read_proposal'])
            if family == 'capacity_unfulfillable':
                expected.update(proposal='infeasible', decision_status='unfulfillable')
            rows.append(c)
    return rows


def setup(case, folder):
    folder.mkdir(parents=True)
    store = ApparelStore(folder / 'operations.sqlite', world=case['world'])
    draft = store.create_draft(OWNER, case['request'])
    if case['initial_selections']:
        draft = store.select(OWNER, draft['id'], case['initial_selections'], expected_revision=draft['revision'])
    if case['approve_sku']:
        draft = store.select(OWNER, draft['id'], [{'line_id': 'primary', 'sku': case['approve_sku']}], expected_revision=draft['revision'])
        approval = draft['order_check']['substitution_proposals'][0]['approval_id']
        draft = store.approve_substitution(OWNER, draft['id'], approval, expected_revision=draft['revision'])
    old = None
    if case['event']:
        old = store.propose(OWNER, draft['id'], expected_revision=draft['revision'], now=instant(case['now']))
        assert old['route']['status'] == 'planned', case['id']
        flight = next(s for s in old['route']['segments'] if s['mode'] == 'air')
        kind = case['event']
        event = {'event_id': 'EV-' + case['id'], 'kind': 'delay' if kind == 'delay' else 'cancel',
                 'leg_id': flight['leg_id'], 'nominal_departure': flight['nominal_departure'], 'published_at': case['now']}
        if kind == 'delay':
            event['delay_minutes'] = 2160
        if kind == 'future':
            event['published_at'] = iso(instant(case['now']) + timedelta(hours=3))
        if kind == 'unrelated':
            leg = next(l for l in store.corridor['legs'] if l['id'] == 'HK-EU-SEA')
            anchor = instant(store.corridor['anchor_at']) + timedelta(minutes=leg['offset_minutes'])
            periods = math.ceil((instant(flight['nominal_departure']) - anchor).total_seconds() / (60 * leg['period_minutes']))
            event.update(leg_id=leg['id'], nominal_departure=iso(anchor + timedelta(minutes=periods * leg['period_minutes'])))
        store.add_transport_event(event)
    current = store.view(OWNER, draft['id'])
    seed = {'draft_id': draft['id'], 'view_digest': digest(current), 'old_id': old['proposal_id'] if old else None,
            'events_digest': digest(store.transport_events())}
    save(folder / 'world.json', case['world'])
    save(folder / 'seed.json', seed)
    return store, seed


def fixture_check(case, initial, seed, scratch):
    store = clone(initial, scratch)
    e = case['expected']
    current = store.view(OWNER, seed['draft_id'])
    assert digest(current) == seed['view_digest']
    if not e['read_only'] and not case['event']:
        selected = [{'line_id': line, 'sku': values[0]} for line, values in e['allowed'].items()]
        current = store.select(OWNER, seed['draft_id'], selected, expected_revision=current['revision'])
    checked = current['order_check']
    assert checked['status'] == e['order_status'], (case['id'], checked['status'], e['order_status'])
    if e['issue']:
        assert e['issue'] in {i['code'] for i in checked['issues']}, case['id']
    if seed['old_id']:
        assert store.assess(OWNER, seed['draft_id'], seed['old_id'], now=instant(case['now']))['valid'] == e['old_valid']
    if e['proposal'] in ('new', 'revise', 'infeasible'):
        proposal = store.propose(OWNER, seed['draft_id'], expected_revision=current['revision'], now=instant(case['now']))
        if e['proposal'] == 'infeasible':
            assert proposal['route']['status'] == 'infeasible' and proposal['route']['adjustment_options']
            weight = sum(case['world']['variants'][sku]['weight_grams_per_catalog_unit'] * q for sku, q in checked['quantities_catalog_units'].items())
            assert weight > 25000 and instant(case['request']['shipping']['deadline_at']) - instant(case['request']['shipping']['ready_at']) < timedelta(days=14)
        else:
            assert proposal['route']['status'] in ('planned', 'not_required') and proposal['independent_route_audit']['passed']
            assert store.assess(OWNER, seed['draft_id'], proposal['proposal_id'], now=instant(case['now']))['valid']
    return {'case_id': case['id'], 'family': case['family'], 'passed': True,
            'scope': 'Free state construction and feasibility checks, not generative model performance.'}

"""New order-state replication over the known catalogue; no unseen-domain claim."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from apparel_fulfillment.data import load_world
from apparel_fulfillment.transport import iso
from research.apparel_cases import eligible_alternatives
from research.apparel_state_cases_v3 import cases as development_cases, line_for


TARGETS = ('us:B06XWPR5RY', 'us:B06XWQBJ6N', 'us:B06XWPMG5D')


def cases():
    world, rows = load_world(), []
    for i, prior in enumerate(development_cases()):
        c = deepcopy(prior)
        sku = TARGETS[i % 3]
        now = datetime(2027, 1, 4, 3, tzinfo=timezone.utc) + timedelta(days=2*i)
        ready = now + timedelta(days=1, hours=2)
        quantity = 32 + 2*(i % 4)
        c.update(id='SCREP-' + c['scenario'], partition='new_order_state_replication', target_sku=sku,
                 now=iso(now), initial_sku=sku, stock_overrides={sku: 120})
        request = c['request']
        request['lines'] = [line_for(world, sku, 'item-1', quantity)]
        expected = c['expected']
        expected.update(selected_skus=[sku], allowed_by_line={'item-1': [sku]})
        if request['needs_shipping']:
            request['shipping'] = {'destination': 'DE-DC', 'ready_at': iso(ready),
                                   'deadline_at': iso(ready+timedelta(days=9)), 'budget_cents': 32000}
        scenario = c['scenario']
        if scenario == 'product_info':
            expected['info_field'] = 'size'
            c['contract'] = {'mode': 'inspect_product', 'product_sku': sku, 'product_fields': ['size']}
            c['task'] = f'请核实商品 {sku} 的尺码并附资料字段；这里只查询资料，保留现有订单。'
        elif scenario == 'order_ready':
            c['initial_sku'] = None
        elif scenario == 'order_clarification':
            request['lines'][0].pop('requested_sku')
            request['lines'][0]['size'] = None
            expected['issue'] = 'size_unspecified'
            c['task'] = '逐项检查当前服装需求是否足够明确。不能把候选规格当成已确认要求；说明还缺哪项条件并等我补充，保持订单不变。'
        elif scenario == 'rule_blocked':
            request['sales_region'] = 'DE'
            request['lines'][0]['quantity'] = 4
            expected['issue'] = 'wholesale_minimum_not_met'
            c['task'] = '核对这笔批量订单是否满足商家销售规则，只报告当前核验状态与具体问题，不增加数量、不修改订单。'
        elif scenario in ('shortage_alternative', 'combined', 'approved_read_only'):
            c['stock_overrides'] = {s: 0 if s == sku else 120 for s in world['variants']}
            alternate_world = deepcopy(world)
            for s, count in c['stock_overrides'].items(): alternate_world['stock'][s]['available_catalog_units'] = count
            eligible, differences = eligible_alternatives(request, sku, alternate_world)
            expected['minimum_differences'] = differences
            if scenario == 'shortage_alternative':
                expected.update(selected_skus=eligible, allowed_by_line={'item-1': eligible})
            else:
                c['approved_sku'] = eligible[0]
                expected.update(selected_skus=[eligible[0]], allowed_by_line={'item-1': [eligible[0]]})
        elif scenario == 'shipping_normal':
            request['shipping'].update(deadline_at=iso(ready+timedelta(days=24)), budget_cents=14500)
        elif scenario == 'shipping_infeasible':
            request['shipping']['deadline_at'] = iso(ready+timedelta(hours=1))
            expected['infeasibility_certificate'] = 'Every origin-to-destination path requires more than one hour even with zero schedule waits.'
        elif scenario == 'shipping_revision':
            c['scenario_kind'] = 'delay'
        elif scenario == 'multiple_lines':
            second = TARGETS[(i+1) % 3]
            request['lines'].append(line_for(world, second, 'item-2', 40))
            c['initial_sku'] = None
            c['initial_selections'] = [{'line_id': 'item-1', 'sku': second}, {'line_id': 'item-2', 'sku': sku}]
            c['stock_overrides'][second] = 120
            expected.update(selected_skus=[sku, second], allowed_by_line={'item-1': [sku], 'item-2': [second]})
        c['replication_notice'] = 'New simulated orders and states, same task families and known catalogue; not independent merchant or unseen-product data.'
        rows.append(c)
    return rows

"""Prototype report contract: model decisions, source-rendered business facts.

This module is separate from the frozen v1 implementation and is not deployed.
It does not use evaluation cases or expected outcomes to construct a report.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, model_validator

from commerce_lab.agent import Args
from commerce_lab_v2.agent import IdentityAwareCommerceAgent


REPORT_VERSION = 'source-rendered-report-v2-prototype'
MissingField = Literal['destination', 'quantity', 'shipping_budget', 'deadline',
                       'product_preference', 'request_details']


class ReportDecision(Args):
    product_ids: list[str] = Field(default_factory=list, max_length=12)
    proposal_id: str | None = None
    status: Literal['completed', 'needs_clarification', 'infeasible']
    missing_fields: list[MissingField] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def clarification_status(self):
        if self.missing_fields and self.status != 'needs_clarification':
            raise ValueError('Missing fields require needs_clarification status')
        return self


def report_language(task: str) -> str:
    """The prototype supports Chinese and English; shipping locale is irrelevant."""
    return 'zh' if re.search(r'[\u3400-\u9fff]', task) else 'en'


def cart_summary(cart: dict) -> dict:
    return {'lines': len(cart['items']),
            'catalog_units': sum(item['quantity'] for item in cart['items']),
            'subtotal_usd': cart['subtotal_usd']}


def render_report(decision: ReportDecision, evidence: dict, language: str) -> str:
    """Keep decisions distinct from observed fields; never infer physical pack size."""
    if language not in {'en', 'zh'}:
        raise ValueError('Supported report languages are en and zh')
    zh = language == 'zh'
    labels = {
        'completed': ('本次处理结果如下。', 'The results of this request are below.'),
        'needs_clarification': ('需要补充信息后才能继续。', 'More information is needed before continuing.'),
        'infeasible': ('本轮未能满足请求的全部条件。', 'This run did not satisfy all requested conditions.'),
    }
    lines = [labels[decision.status][0 if zh else 1]]
    questions = {
        'destination': ('请提供配送目的地。', 'Please provide the shipping destination.'),
        'quantity': ('请提供所需目录单位数量。', 'Please provide the requested quantity in catalog units.'),
        'shipping_budget': ('请提供运费预算（USD）。', 'Please provide the shipping budget in USD.'),
        'deadline': ('请提供最晚送达期限。', 'Please provide the delivery deadline.'),
        'product_preference': ('请补充商品选择要求。', 'Please clarify your product preferences.'),
        'request_details': ('请澄清需要继续处理的具体要求。', 'Please clarify the request details needed to continue.'),
    }
    if decision.status == 'needs_clarification':
        for field in dict.fromkeys(decision.missing_fields or ['request_details']):
            lines.append(questions[field][0 if zh else 1])
    if decision.product_ids:
        lines.append(('商品ID：' if zh else 'Product IDs: ') + ', '.join(decision.product_ids))
    before, after = evidence['cart_before'], evidence['cart_after']
    bs, es = cart_summary(before), cart_summary(after)
    if zh:
        lines.append(f"购物车：本轮开始时 {bs['lines']} 个条目、{bs['catalog_units']} 个目录单位；"
                     f"结束时 {es['lines']} 个条目、{es['catalog_units']} 个目录单位。"
                     f"结束小计 USD {es['subtotal_usd']:.2f}（模拟价格）。")
    else:
        lines.append(f"Cart at the start of this run: {bs['lines']} lines, {bs['catalog_units']} catalog units. "
                     f"At the end: {es['lines']} lines, {es['catalog_units']} catalog units. "
                     f"Final subtotal: USD {es['subtotal_usd']:.2f} (synthetic prices).")
    # Counts do not imply unchanged identity/quantities: compare the actual rows.
    unchanged = before['items'] == after['items']
    lines.append(('本轮购物车内容未变。' if zh else 'Cart contents were unchanged during this run.') if unchanged
                 else ('本轮购物车内容发生了变化，当前条目见购物车。' if zh
                       else 'Cart contents changed during this run; the cart shows the current items.'))
    quote = evidence.get('quote')
    if quote:
        plan, request = quote['plan'], quote['request']
        if zh:
            lines.append(f"已查询运输：目的地 {request['destination']}；期限 {request['deadline_days']} 天；"
                         f"运费预算 USD {request['budget_usd']:.2f}。")
        else:
            lines.append(f"Shipping was queried for {request['destination']}, within {request['deadline_days']} days, "
                         f"with a USD {request['budget_usd']:.2f} shipping budget.")
        if plan['status'] == 'planned':
            route = ', '.join(plan['leg_ids'])
            lines.append((f"模拟运输路线 {route}；运费 USD {plan['total_cost_usd']:.2f}；运输时间 {plan['transit_days']} 天。"
                          if zh else f"Synthetic route: {route}; shipping USD {plan['total_cost_usd']:.2f}; "
                          f"transit time {plan['transit_days']} days."))
        else:
            lines.append('运输工具未找到同时满足所给约束的单仓路线。' if zh
                         else 'The shipping tool found no single-warehouse route satisfying the supplied constraints.')
        if request.get('blocked_legs'):
            lines.append(('此次查询排除的路线：' if zh else 'Legs excluded in this query: ') + ', '.join(request['blocked_legs']))
    else:
        lines.append('本轮没有进行运输报价。' if zh else 'No shipping quote was requested during this run.')
    proposals = evidence.get('staged_proposal_ids', [])
    lines.append((('本轮已创建的本地模拟提案：' if zh else 'Local simulation proposals staged in this run: ')
                  + ', '.join(proposals)) if proposals else
                 ('本轮没有创建订单提案。' if zh else 'No order proposal was staged in this run.'))
    lines.append('提案须由你单独确认后才生成模拟订单；这里不执行真实支付或发货。' if zh
                 else 'Your separate confirmation is required to turn a proposal into a simulation order; no live payment or delivery is performed here.')
    for ident, record in evidence.get('stocks', {}).items():
        values = '; '.join(f"{row['warehouse']}={row['quantity']}" for row in record['warehouses'])
        lines.append(('已观察的模拟库存（目录单位）' if zh else 'Observed synthetic stock (catalog units) ')
                     + ident + ': ' + values)
    # The source record is rendered as named fields/quotes, not converted into
    # guessed pack counts, material composition, guarantees or variant choices.
    optional = []
    for ident in decision.product_ids:
        row = evidence.get('details', {}).get(ident)
        if not row:
            continue
        optional.append(('商品目录资料 ' if zh else 'Catalog record ') + ident)
        for key, en, cn in [('title', 'Title', '标题'), ('brand', 'Brand', '品牌')]:
            if row.get(key):
                optional.append(f"{cn if zh else en}: {row[key]}")
        color = row.get('attributes', {}).get('color')
        if color:
            optional.append(f"{'目录颜色字段' if zh else 'Catalog color field'}: {color}")
        if row.get('price') is not None:
            optional.append(f"{'模拟研究价格' if zh else 'Synthetic research price'}: {row['currency']} {row['price']:.2f}")
        description = row.get('long_description') or row.get('short_description')
        if description:
            optional.append(('目录描述原文摘录（可能截断）：' if zh else 'Verbatim catalog-description excerpt (may be truncated): ')
                            + json.dumps(description[:400], ensure_ascii=False))
    for tool_name, rows in evidence.get('service_records', []):
        optional.append(('已查询的服务记录 ' if zh else 'Retrieved service record ') + tool_name + ': '
                        + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    # Legacy answer limits remain intact. Full factual records are also retained
    # in the result, even when the compact narrative cannot include every field.
    text = '\n'.join(lines)
    if len(text) > 3500:
        raise ValueError('Essential report exceeds the supported compact-report size')
    omitted = False
    for line in optional:
        if len(text) + len(line) + 1 <= 3860:
            text += '\n' + line
        else:
            omitted = True
    if omitted:
        text += ('\n部分资料未放入简短说明，完整来源保留在本轮记录中。' if zh
                 else '\nSome source fields are omitted from this compact view; full records remain in this run.')
    return text


class StructuredReportAgent(IdentityAwareCommerceAgent):
    def __init__(self, *args, language: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if language is not None and language not in {'en', 'zh'}:
            raise ValueError('Supported report languages are en and zh')
        self.language_override = language

    def base_prompt(self, role):
        original = super().base_prompt(role)
        if role != 'coordinator':
            return original
        prefix = original.split('Return ONLY JSON when finished:', 1)[0]
        return prefix + ('Return ONLY JSON when finished, with exactly these fields: '
            '{"product_ids":["observed IDs"],"proposal_id":null,'
            '"status":"completed|needs_clarification|infeasible","missing_fields":[]}. '
            'Do not provide an answer or narrative: the host renders source facts and actual state changes. '
            'When clarification is required, identify only the genuinely missing values using these names: '
            'destination, quantity, shipping_budget, deadline, product_preference, request_details. '
            'Otherwise missing_fields must be empty. Include the requested observed product IDs. '
            'A staged proposal is not a confirmed order. Use only a proposal ID returned in this run.')

    async def execute_tool(self, name, args):
        output = await super().execute_tool(name, args)
        if isinstance(output, dict) and output.get('error'):
            return output
        if name == 'get_product_details' and isinstance(output, dict) and output.get('product_id'):
            self.report_evidence['details'][output['product_id']] = output
        elif name == 'search_products' and isinstance(output, list):
            for row in output:
                if row.get('product_id'):
                    self.report_evidence['details'][row['product_id']] = row
        elif name == 'get_stock' and isinstance(output, dict) and output.get('product_id'):
            self.report_evidence['stocks'][output['product_id']] = output
        elif name == 'stage_order' and isinstance(output, dict) and output.get('proposal_id'):
            self.report_evidence['staged_proposal_ids'].append(output['proposal_id'])
        elif name in {'search_policies', 'get_orders', 'get_order'}:
            self.report_evidence['service_records'].append((name, output))
        return output

    async def loop(self, role, task, *, max_steps):
        text = await super().loop(role, task, max_steps=max_steps)
        if role != 'coordinator':
            return text
        if text.strip().startswith('```'):
            text = '\n'.join(text.strip().splitlines()[1:-1])
        decision = ReportDecision.model_validate_json(text)
        self.report_evidence['cart_after'] = self.store.cart(self.session.session_id)
        self.report_evidence['quote'] = self.last_quote
        self.report_evidence['decision'] = decision.model_dump()
        self.store.trace(self.run_id, 'host', 'report_source_records', self.report_evidence)
        return json.dumps({'answer': render_report(decision, self.report_evidence, self.output_language),
                           'product_ids': decision.product_ids, 'proposal_id': decision.proposal_id,
                           'status': decision.status}, ensure_ascii=False)

    async def run(self, session_id, task, run_id=None):
        self.store.session(session_id)
        self.output_language = self.language_override or report_language(task)
        self.report_evidence = {'version': REPORT_VERSION, 'language': self.output_language,
                               'cart_before': self.store.cart(session_id), 'details': {}, 'stocks': {},
                               'staged_proposal_ids': [], 'service_records': []}
        result = await super().run(session_id, task, run_id)
        if 'error' not in result:
            result['report_evidence'] = self.report_evidence
            result['report_contract_version'] = REPORT_VERSION
            self.store.update_run(result['run_id'], 'completed', result)
        return result

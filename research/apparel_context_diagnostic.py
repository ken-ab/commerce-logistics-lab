"""Descriptive, zero-call audit of frozen apparel transcripts. Never rewrites scores."""
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / 'evidence/apparel_strategy_v1'
ARMS = ('single', 'coordinator', 'on_demand')
LABELS = {'single': '单 Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}
READ_TOOLS = frozenset(('read_order', 'read_variant', 'search_variants', 'find_alternatives',
                        'read_transport_events', 'read_proposal'))
GAPS = {'ready_order_status_not_cited', 'current_route_arrival_at_not_cited',
        'current_route_total_cost_cents_not_cited'}
VARIANT_LAYOUT_ERRORS = {'/result/variant/stock/available_catalog_units',
                        '/result/variant/brand_rule/allowed_sales_regions',
                        '/result/variant/brand_rule/wholesale_minimum_pieces_per_sku'}
VERSION = 'apparel-context-diagnostic-v1'


def compact(value, *, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=sort_keys)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode(content):
    if not isinstance(content, str):
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def nested_observations(value):
    """Only complete observation records, not citations or claimed facts."""
    if isinstance(value, dict):
        if {'observation_id', 'tool', 'success', 'result'} <= value.keys():
            yield value
        else:
            for item in value.values():
                yield from nested_observations(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_observations(item)


def repeated_reads(traces):
    """Compare each successful read to the previous successful read of that key.

    Same values do NOT establish that a freshness check was unnecessary.
    Changed results and different arguments are never merged.
    """
    latest, duplicates, count = {}, [], 0
    for index, t in enumerate(traces):
        if t['kind'] != 'tool' or not t['success'] or t['tool'] not in READ_TOOLS:
            continue
        count += 1
        key = (t['tool'], compact(t['arguments'], sort_keys=True))
        value = compact(t['result'], sort_keys=True)
        previous = latest.get(key)
        if previous and previous['value'] == value:
            intervening_writes = sum(x['kind'] == 'tool' and x['tool'] not in READ_TOOLS
                                     for x in traces[previous['index'] + 1:index])
            duplicates.append({'tool': t['tool'], 'previous_observation': previous['id'],
                               'observation_id': t['observation_id'],
                               'previous_role': previous['role'], 'role': t['role'],
                               'cross_role': previous['role'] != t['role'],
                               'intervening_write_attempts': intervening_writes})
        latest[key] = {'value': value, 'id': t['observation_id'], 'role': t['role'], 'index': index}
    return count, duplicates


def context_measurements(traces, root_role):
    totals = Counter()
    unique_handoffs = {}
    last_root_observations = {}
    per_role = Counter()
    for t in traces:
        if t['kind'] != 'model':
            continue
        messages = t['messages']
        original = compact(messages)
        if len(original) != t['input_characters']:
            raise ValueError('Recorded input character count differs from original messages')
        if hashlib.sha256(compact(messages, sort_keys=True).encode('utf-8')).hexdigest() != t['input_sha256']:
            raise ValueError('Recorded message digest differs')
        totals['model_inputs'] += 1
        totals['message_characters'] += len(original)
        per_role[t['role']] += len(original)
        candidate = deepcopy(messages)
        root_visible = {}
        for m, reduced in zip(messages, candidate):
            value = decode(m.get('content'))
            if value is None:
                continue
            if t['role'] == root_role:
                for obs in nested_observations(value):
                    root_visible[obs['observation_id']] = obs
            if m['role'] == 'user' and isinstance(value, dict) and 'shared_observations' in value:
                totals['shared_observation_characters_across_inputs'] += len(compact(value['shared_observations']))
            if m['role'] != 'tool' or not isinstance(value, dict) or 'expert_report' not in value:
                continue
            totals['handoff_message_appearances'] += 1
            ident = m['tool_call_id']
            signature = compact(value)
            if ident in unique_handoffs and unique_handoffs[ident]['signature'] != signature:
                raise ValueError('One tool call ID has inconsistent handoff results')
            unique_handoffs[ident] = {'signature': signature, 'report_missing': value['expert_report'] is None,
                                     'new_observations': len(value.get('new_observations', []))}
            if isinstance(value['expert_report'], dict) and 'answer' in value['expert_report']:
                # A representation-only counterfactual: no calls, scores or source facts change.
                del value['expert_report']['answer']
                reduced['content'] = compact(value)
        if t['role'] == root_role:
            last_root_observations = root_visible
        totals['rendered_answer_removal_characters'] += len(original) - len(compact(candidate))
    totals['logical_handoffs_visible_to_root'] = len(unique_handoffs)
    totals['logical_handoffs_without_report'] = sum(h['report_missing'] for h in unique_handoffs.values())
    return dict(totals), dict(per_role), last_root_observations


def available_gap_fields(run, score, visible):
    """Annotate just three pre-existing gaps; this is not a new correctness scorer."""
    report = run.get('report') or {}
    proposal_id = report.get('decision', {}).get('proposal_id')
    proposal = next((p for p in run['after']['proposals'] if p['proposal_id'] == proposal_id), None)
    rows = []
    for gap in sorted(set(score['evidence_gaps']) & GAPS):
        found = []
        for ident, obs in visible.items():
            if not obs['success']:
                continue
            result = obs['result']
            if gap == 'ready_order_status_not_cited':
                actual = result.get('order_check', {}).get('status')
                expected = run['after']['order_check']['status']
                if actual is not None and actual == expected:
                    found.append({'observation_id': ident, 'pointer': '/result/order_check/status', 'value': actual})
            elif proposal:
                observed = result.get('proposal', result)
                field = ('arrival_at' if gap == 'current_route_arrival_at_not_cited' else 'total_cost_cents')
                actual = observed.get('route', {}).get(field)
                if observed.get('proposal_id') == proposal_id and actual is not None and actual == proposal['route'].get(field):
                    prefix = '/result/proposal' if 'proposal' in result else '/result'
                    found.append({'observation_id': ident, 'pointer': prefix + '/route/' + field, 'value': actual})
        rows.append({'gap': gap, 'has_completed_report': bool(report), 'available_in_last_root_input': bool(found),
                     'observed_fields': found})
    return rows


def stock_review_flags(run):
    report = run.get('report') or {}
    selected = set(report.get('decision', {}).get('product_skus', []))
    flags = []
    for fact in report.get('source_facts', []):
        observation = run['observations'].get(fact['observation_id'], {})
        source_sku = observation.get('result', {}).get('variant', {}).get('sku')
        if fact['pointer'].startswith('/result/stock/') and source_sku and selected and source_sku not in selected:
            flags.append({'observation_id': fact['observation_id'], 'pointer': fact['pointer'],
                          'source_sku': source_sku, 'reported_skus': sorted(selected),
                          'notice': 'Review only: citing original stock can be a legitimate comparison.'})
    return flags


def diagnose(run, score):
    count, repeated = repeated_reads(run['traces'])
    context, roles, visible = context_measurements(run['traces'], run['arm'])
    invalid_fields = Counter()
    invalid_reasons = Counter()
    repair_roles = Counter()
    layout_repairs = 0
    for t in run['traces']:
        if t['kind'] == 'report_rejected':
            repair_roles[t['role']] += 1
            layout_repairs += any(e.get('pointer') in VARIANT_LAYOUT_ERRORS for e in t['invalid'])
            for invalid in t['invalid']:
                invalid_fields[invalid.get('pointer', invalid.get('field', '<unspecified>'))] += 1
                invalid_reasons[invalid['reason']] += 1
    gaps = available_gap_fields(run, score, visible)
    return {'case_id': run['case_id'], 'arm': run['arm'], 'family': run['family'],
            'supplementary_task_completed': score['task_completed'], 'run_status': run['run_status'],
            'input_tokens': run['input_tokens'], 'output_tokens': run['output_tokens'],
            'cost_cny': run['accounted_and_reserved_cny'],
            'successful_read_calls': count, 'unchanged_repeated_reads': len(repeated),
            'cross_role_unchanged_repeated_reads': sum(r['cross_role'] for r in repeated),
            'repeated_read_records': repeated, 'context': context, 'message_characters_by_role': roles,
            'report_repair_attempts': sum(repair_roles.values()), 'report_repairs_by_role': dict(repair_roles),
            'variant_layout_repair_attempts': layout_repairs,
            'invalid_field_frequencies': dict(invalid_fields), 'invalid_reason_frequencies': dict(invalid_reasons),
            'selected_gap_availability': gaps, 'stock_citation_review_flags': stock_review_flags(run)}


def aggregate(rows):
    out = {}
    for arm in ARMS:
        selected = [r for r in rows if r['arm'] == arm]
        context, roles, fields, reasons, repairs = Counter(), Counter(), Counter(), Counter(), Counter()
        for r in selected:
            context.update(r['context']); roles.update(r['message_characters_by_role'])
            fields.update(r['invalid_field_frequencies']); reasons.update(r['invalid_reason_frequencies'])
            repairs.update(r['report_repairs_by_role'])
        gaps = [g for r in selected for g in r['selected_gap_availability']]
        gap_counts = {}
        for gap in sorted(GAPS):
            items = [g for g in gaps if g['gap'] == gap]
            gap_counts[gap] = {'gap_runs': len(items),
                              'completed_report_with_field_in_context': sum(g['has_completed_report'] and g['available_in_last_root_input'] for g in items)}
        out[arm] = {'runs': len(selected),
                    **{k: sum(r[k] for r in selected) for k in ('successful_read_calls', 'unchanged_repeated_reads',
                         'cross_role_unchanged_repeated_reads', 'report_repair_attempts', 'variant_layout_repair_attempts', 'input_tokens', 'output_tokens')},
                    'runs_with_unchanged_repeated_reads': sum(bool(r['repeated_read_records']) for r in selected),
                    'runs_with_stock_citation_review_flags': sum(bool(r['stock_citation_review_flags']) for r in selected),
                    'context': dict(context), 'message_characters_by_role': dict(roles),
                    'report_repairs_by_role': dict(repairs), 'invalid_field_frequencies': dict(fields.most_common()),
                    'invalid_reason_frequencies': dict(reasons.most_common()), 'selected_gap_availability': gap_counts}
    return out


def make():
    # Verify the same immutable source set as the released supplementary analysis.
    method_path = STUDY / 'method.json'
    method = json.loads(method_path.read_text(encoding='utf-8'))
    for name, expected in method['method_files_sha256'].items():
        if sha(ROOT / name) != expected:
            raise ValueError('Frozen method changed: ' + name)
    audit_path = STUDY / 'test/task_audit_v3.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    if audit['status'] != 'complete' or len(audit['source_results_sha256']) != 324:
        raise ValueError('A complete frozen 324-run source set is required')
    scores = {(c['case_id'], c['arm']): c['score'] for c in audit['cases']}
    rows = []
    for name, expected in sorted(audit['source_results_sha256'].items()):
        path = (ROOT / name).resolve()
        path.relative_to((STUDY / 'test').resolve())
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Frozen result changed: ' + name)
        run = json.loads(raw)
        rows.append(diagnose(run, scores[(run['case_id'], run['arm'])]))
    if Counter(r['arm'] for r in rows) != Counter({a: 108 for a in ARMS}):
        raise ValueError('Expected 108 runs in each arm')
    arms = aggregate(rows)
    output = {'version': VERSION, 'observed_on': '2026-09-08',
              'scope': 'Post-hoc descriptive analysis of frozen v1 test data; zero new model calls; no rescore or performance improvement claim.',
              'new_model_calls': 0, 'incremental_api_cost_cny': '0',
              'method_sha256': sha(method_path), 'source_audit_sha256': sha(audit_path),
              'diagnostic_code_sha256': sha(Path(__file__)),
              'source_results_sha256': audit['source_results_sha256'], 'arms': arms, 'cases': rows}
    path = ROOT / 'evidence/apparel_context_diagnostic_20260908.json'
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    csv_path = path.with_name('apparel_context_diagnostic_20260908_cases.csv')
    fields = ('case_id', 'arm', 'family', 'supplementary_task_completed', 'run_status', 'input_tokens', 'output_tokens',
              'cost_cny', 'successful_read_calls', 'unchanged_repeated_reads', 'cross_role_unchanged_repeated_reads',
              'report_repair_attempts', 'message_characters', 'rendered_answer_removal_characters',
              'logical_handoffs_visible_to_root', 'logical_handoffs_without_report', 'stock_review_flags')
    with csv_path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for row in rows:
            flat = row | row['context'] | {'stock_review_flags': len(row['stock_citation_review_flags'])}
            writer.writerow({key: flat.get(key, 0) for key in fields})
    write_report(output)
    print(json.dumps({'runs': len(rows), 'new_model_calls': 0, 'output': str(path),
                      'unchanged_repeated_reads': {a: v['unchanged_repeated_reads'] for a, v in arms.items()}}, ensure_ascii=False))


def write_report(output):
    text = ['# 服装 Agent 上下文诊断：重复信息与最终引用遗漏', '',
            '本报告重新读取已冻结的324条测试轨迹，未调用模型、未改业务代码、未重算完成率，新增API费用为0元。它用于提出下一轮假设，不能作为独立测试或改进结果。', '',
            '诊断改变了优化优先级：删除专家展示文本只减少协调员消息字符的0.94%，不能解释其主要开销。256次报告修复中，175次包含把库存或品牌规则误放在`variant`内部的引用。先验证可引用字段目录与明确的专家子任务边界，比优先引入有损压缩更有依据；这仍是待实验检验的判断。', '',
            '## 重复读取', '',
            '同一次运行内，比较同工具、相同参数的前后两次成功只读调用；完整返回JSON值相同才计数。参数不同、库存或版本改变不计为相同。重复返回并不证明再次检查没有必要，因此这里不称其为“浪费调用”。', '',
            '| 策略 | 成功只读调用 | 返回未变的重复读取 | 涉及任务 /108 | 跨执行角色的重复 |', '|---|---:|---:|---:|---:|']
    for arm, a in output['arms'].items():
        text.append(f'| {LABELS[arm]} | {a["successful_read_calls"]} | {a["unchanged_repeated_reads"]} | {a["runs_with_unchanged_repeated_reads"]} | {a["cross_role_unchanged_repeated_reads"]} |')
    text += ['', '## 交接内容的重复表示', '',
             '每次模型输入都按原记录计量；同一条历史消息再次发送会再次计入。字符是JSON序列化后的Unicode字符数，**不是token**，且消息字符不含工具定义或服务端开销。token列来自真实调用usage，不能按字符比例折算费用节省。', '',
             '| 策略 | 实际输入token | 消息字符总数 | 仅移除专家展示文本的字符差 | 占消息字符 | 可见专家交接 / 无报告 |', '|---|---:|---:|---:|---:|---:|']
    for arm, a in output['arms'].items():
        c = a['context']; delta = c.get('rendered_answer_removal_characters', 0)
        text.append(f'| {LABELS[arm]} | {a["input_tokens"]:,} | {c["message_characters"]:,} | {delta:,} | {delta/c["message_characters"]:.2%} | {c["logical_handoffs_visible_to_root"]} / {c["logical_handoffs_without_report"]} |')
    text += ['', '上述字符差是离线删除专家交接中的`expert_report.answer`字段所得，其余`decision`、`source_facts`、`grounding`和原始观察保留。展示文本由已有字段展开，值得验证能否只在界面生成。该计算没有重新运行模型，不证明删掉文本后模型的行为、费用或准确率保持不变。', '',
             '“可见交接”只统计确实进入后续模型输入的工具回复，按`tool_call_id`去重；没有形成报告的交接仍可能传回原始观察，不等于其所有工作都丢失。', '',
             '## 证据已经可见，但最终没有引用', '',
             '只检查原补充评分中的三类引用缺口，并在最后一次根执行者输入中寻找对应原始观察。运输字段必须匹配最终所报提案ID和当前字段值；订单状态必须匹配最终核验状态。这里计量信息可得性，不重新判分，也不作因果解释。', '',
             '| 策略 | 订单状态缺口：已可见 / 全部 | 到达时间缺口：已可见 / 全部 | 运输费用缺口：已可见 / 全部 |', '|---|---:|---:|---:|']
    for arm, a in output['arms'].items():
        text.append('| ' + LABELS[arm] + ' | ' + ' | '.join(f'{a["selected_gap_availability"][g]["completed_report_with_field_in_context"]} / {a["selected_gap_availability"][g]["gap_runs"]}' for g in ('ready_order_status_not_cited', 'current_route_arrival_at_not_cited', 'current_route_total_cost_cents_not_cited')) + ' |')
    text += ['', '分子要求已形成最终报告；没有完成报告的运行另保留在逐例JSON中。0个引用缺口的类别不计算比例。多种缺口可出现在同一任务，不能把列相加当作失败任务数。', '',
             '## 引用修复与待人工核对项', '',
             '| 策略 | 引用修复尝试 | 含库存/品牌规则层级错误 | 库存引用SKU与最终候选不同的任务 |', '|---|---:|---:|---:|']
    for arm, a in output['arms'].items():
        text.append(f'| {LABELS[arm]} | {a["report_repair_attempts"]} | {a["variant_layout_repair_attempts"]} | {a["runs_with_stock_citation_review_flags"]} |')
    text += ['', '库存SKU不一致仅是复核标记：引用原商品库存来解释替代完全可能合理，不能自动判错。JSON包含每次观察ID、指针、来源SKU和最终候选，须结合实际陈述阅读。引用修复按被拒的`finish`尝试计次；一次尝试可含多个错误字段。', '']
    for arm, a in output['arms'].items():
        top = '；'.join(f'`{k}` {v}次' for k, v in list(a['invalid_field_frequencies'].items())[:5])
        text.append(f'- {LABELS[arm]}最常见无效字段：{top}。')
    text += ['', '## 下一轮怎样验证', '',
             '1. 先提供由实际观察生成的可引用字段目录，单独检验能否减少引用层级错误。目录不能生成新事实或替模型执行遗漏的业务动作。另行检验专家只完成受委派子任务、及时返回的边界设计；不要混合改动后声称已识别单一原因。',
             '2. 对最终报告增加与订单、SKU和提案版本绑定的字段检查；把“指针存在”和“引用回答了当前问题”分开。完整交接与只保留结构化交接可作次要消融，用实际token、费用、时延和完成率判断，而非以字符比例代替。',
             '3. 使用旧测试仅作开发诊断；新的正式对照要另行冻结未用案例和评分，保留原v1结果。暂不引入有损提示压缩或新训练模型。', '',
             '方法依据：LangChain官方[Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)强调输入上下文与返回结果都需设计；[LLMLingua-2](https://arxiv.org/html/2403.12968v2)与[RECOMP](https://arxiv.org/html/2310.04408v1)研究压缩后的任务表现。这些论文并未在本项目复现，不能把论文加速结果写成项目成果。', '',
             '复现：`python -X utf8 -m research.apparel_context_diagnostic`。脚本逐个核对324个原始结果和方法文件SHA-256，再核对每次模型消息摘要与字符数。', '',
             '- [逐例JSON与原始文件摘要](../evidence/apparel_context_diagnostic_20260908.json)',
             '- [324行CSV](../evidence/apparel_context_diagnostic_20260908_cases.csv)',
             '- [原始阶段结果](APPAREL_RESULTS.md)',
             '- [七个代表性失败的人工复核](APPAREL_FAILURE_REVIEW.md)', '']
    (ROOT / 'research/APPAREL_CONTEXT_DIAGNOSTIC_20260908.md').write_text('\n'.join(text), encoding='utf-8')


if __name__ == '__main__':
    make()

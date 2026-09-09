"""Report the complete registered development pilot without selecting a winner."""
from collections import Counter
import csv
from decimal import Decimal
import json

from apparel_fulfillment.data import ROOT
from research.apparel_analysis import aggregate
from research.apparel_context_diagnostic import VARIANT_LAYOUT_ERRORS
from research.apparel_evidence_pilot import DIRECTORY, CONDITIONS, validate_pilot
from research.apparel_experiment import sha, save
from research.apparel_report import LABELS, FAMILIES


def make():
    registration = validate_pilot()
    summary_path = DIRECTORY / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if summary['runs'] != 54 or len(summary['source_results_sha256']) != 54:
        raise ValueError('Only a complete planned pilot can be reported')
    rows = []
    for name, expected in sorted(summary['source_results_sha256'].items()):
        p = ROOT / name
        if sha(p) != expected: raise ValueError('Pilot raw result changed: ' + name)
        rows.append(json.loads(p.read_text(encoding='utf-8')))
    expected_jobs = {tuple(j) for j in registration['jobs']}
    if {(r['case_id'], r['condition'], r['arm']) for r in rows} != expected_jobs:
        raise ValueError('Pilot result set differs from registration')
    for case_id in {r['case_id'] for r in rows}:
        if len({r['initial_view_digest'] for r in rows if r['case_id'] == case_id}) != 1:
            raise ValueError('Paired initial state differs')
    totals = {c: aggregate([r | {'score': r['task_audit']} for r in rows if r['condition'] == c]) for c in CONDITIONS}
    pairs = {}
    for arm in LABELS:
        original = {r['case_id']: r for r in rows if r['arm'] == arm and r['condition'] == 'original'}
        modified = {r['case_id']: r for r in rows if r['arm'] == arm and r['condition'] == 'directory'}
        pairs[arm] = dict(Counter('improved' if modified[k]['task_audit']['task_completed'] > original[k]['task_audit']['task_completed']
                                  else 'regressed' if modified[k]['task_audit']['task_completed'] < original[k]['task_audit']['task_completed']
                                  else 'tied' for k in original))
    layout = {c: sum(t['kind'] == 'report_rejected' and any(e.get('pointer') in VARIANT_LAYOUT_ERRORS for e in t['invalid'])
                     for r in rows if r['condition'] == c for t in r['traces']) for c in CONDITIONS}
    cost = sum((Decimal(r['accounted_and_reserved_cny']) for r in rows), Decimal(0))
    save(DIRECTORY / 'comparison.json', {'scope': 'development_only', 'runs': 54, 'totals': totals, 'paired_outcomes': pairs,
                                        'variant_layout_repair_attempts': layout, 'cost_cny': str(cost),
                                        'source_summary_sha256': sha(summary_path)})
    with (DIRECTORY / 'case_results.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        fields = ['case_id', 'family', 'arm', 'condition', 'run_status', 'original_strict_completed', 'task_completed',
                  'business_success', 'required_evidence_covered', 'constraint_violation', 'report_repairs', 'model_calls',
                  'input_tokens', 'output_tokens', 'cost_cny', 'latency_seconds', 'failure_reasons', 'evidence_gaps']
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for r in rows:
            s = r['task_audit']
            out = {k: r[k] for k in ('case_id', 'family', 'arm', 'condition', 'run_status', 'model_calls', 'input_tokens', 'output_tokens', 'latency_seconds')}
            out.update({k: s[k] for k in ('task_completed', 'business_success', 'required_evidence_covered', 'constraint_violation')})
            out.update(original_strict_completed=r['score']['task_completed'], report_repairs=s['report_repair_attempts'],
                       cost_cny=r['accounted_and_reserved_cny'], failure_reasons=';'.join(s['failure_reasons']), evidence_gaps=';'.join(s['evidence_gaps']))
            writer.writerow(out)
    base, new = totals['original'], totals['directory']
    text = ['# 引用字段目录：54次开发试跑结果', '',
            f'新目录版在这9个开发案例、三种策略共27次执行中完成 **{new["task_completed"]}/27**；同期原版完成 **{base["task_completed"]}/27**。两个版本合计54次，费用与预留合计 **{cost}元**。以下结论仅限本次开发试跑，不替代原324次正式研究。', '',
            '## 固定条件与改动', '',
            '唯一干预是工具观察增加可复制的字段路径目录。原始业务事实、工具权限、模型、提示、策略及调用上限保持不变。目录不读取期望标签；同一案例6个运行复制相同初始库，运行顺序预先随机登记。9个案例仍使用已知的小型商品目录，仅新增模拟任务组合、数量与日期；不声称新的真实商家或商品分布。', '',
            '54次全部保留，未按中间分数换提示或重试。免费环境检查9例、已有观察1254条/目录路径9757条均通过；这些免费检查不计为模型成功。预检查中一次无运输状态断言错误在付费前修复，旧现场仍保留。', '',
            '| 策略 | 版本 | 完成/9：补充口径 | 原严格口径 | 平均费用 CNY | 平均时延 s | 输入token总量 | 引用修复 |', '|---|---|---:|---:|---:|---:|---:|---:|']
    for arm in LABELS:
        for condition in CONDITIONS:
            a = summary['arms'][condition][arm]; s = a['supplementary']
            text.append(f'| {LABELS[arm]} | {"原版" if condition == "original" else "字段目录"} | {s["task_completed"]} | {a["original_strict"]["task_completed"]} | {s["avg_cost_cny"]:.5f} | {s["avg_latency_seconds"]:.2f} | {s["input_tokens_known"]:,} | {s["report_repair_attempts"]} |')
    text += ['', '主读数采用之前已公开的补充任务口径，同时保留原严格清单；二者定义与先前报告一致，此次没有修改评分。完成要求业务结果和必要来源同时合格；HTTP请求成功不能代替任务完成。', '',
             '| 汇总指标 | 原版27次 | 目录版27次 |', '|---|---:|---:|',
             f'| 业务结果正确 | {round(base["business_success"] * 27)}/27 | {round(new["business_success"] * 27)}/27 |']
    metrics = [('实际输入token', 'input_tokens_known', ',d'), ('实际输出token', 'output_tokens_known', ',d'),
               ('模型调用', 'model_calls', 'd'), ('最终约束违规任务', 'constraint_violation_runs', 'd'),
               ('平均费用 CNY', 'avg_cost_cny', '.5f'), ('平均时延 s', 'avg_latency_seconds', '.2f'),
               ('P95时延 s', 'p95_latency_seconds', '.2f'), ('引用修复次数', 'report_repair_attempts', 'd'),
               ('无效工具尝试', 'rejected_tool_attempts', 'd')]
    for label, key, fmt in metrics:
        text.append(f'| {label} | {format(base[key], fmt)} | {format(new[key], fmt)} |')
    text += [f'| 含库存/规则层级错误的修复 | {layout["original"]} | {layout["directory"]} |', '',
             '## 配对差异与边界', '']
    for arm, p in pairs.items():
        text.append(f'- {LABELS[arm]}：目录版由失败变成功 {p.get("improved", 0)}例，由成功变失败 {p.get("regressed", 0)}例，完成状态相同 {p.get("tied", 0)}例。')
    text += ['', '每种策略只有9例，且一类任务只有1例，不能判断各业务类别的稳定优劣；同一案例的三种策略也不是三个独立业务样本。费用和时延来自实际API运行，包含目录增量及不同执行轨迹的共同作用；没有测量服务端纯思考时间，不能把端到端时延当思考时长。', '',
             '目录让特定引用错误更少，但业务结果并未随之改善。人工复核看到：把草稿ID当成提案ID后没有读取订单恢复；文字称已选入替代，实际未调用选择工具；已有订单状态没有进入最终引用。见[三组退步的逐例复核](APPAREL_EVIDENCE_PILOT_FAILURE_REVIEW.md)。这些是后续假设，不把它们全部归因为目录这一单一因素。', '',
             '当前工作台保留已验收的v1和原验证默认。这轮仅为开发证据，不能直接替换默认或宣称已找到最终最优编排。若继续正式比较，应在此次开发后冻结新的未用任务、代码及评分。', '',
             '## 全部未完成任务', '',
             '| 案例 | 策略 | 版本 | 业务错误 | 证据缺口 |', '|---|---|---|---|---|']
    failures = 0
    for r in rows:
        s = r['task_audit']
        if s['task_completed']: continue
        failures += 1
        text.append(f'| {FAMILIES[r["family"]]} | {LABELS[r["arm"]]} | {r["condition"]} | {"; ".join(s["failure_reasons"]) or "无"} | {"; ".join(s["evidence_gaps"]) or "无"} |')
    if not failures: text.append('| 本轮未观察到 | — | — | — | — |')
    text += ['', '上述失败均留在分母，自动字段检查未覆盖自由理由的逐句真实性，不能据此称全文事实准确率100%。', '',
             f'完成时全项目账本费用与预留为 **{summary["ledger_after"]["accounted_and_reserved_cny"]}元**，额度480元；这是本地调用核算，不是平台账单。Finance-Agent未实验或修改，100模型选型未重跑。', '',
             '- [预先登记协议](APPAREL_EVIDENCE_PILOT_PROTOCOL.md)',
             '- [54行逐例CSV](../evidence/apparel_evidence_pilot_v2/case_results.csv)',
             '- [完整统计与54条源文件摘要](../evidence/apparel_evidence_pilot_v2/summary.json)',
             '- [免费上下文诊断](APPAREL_CONTEXT_DIAGNOSTIC_20260908.md)', '']
    (ROOT / 'research/APPAREL_EVIDENCE_PILOT_RESULTS.md').write_text('\n'.join(text), encoding='utf-8')
    print(json.dumps({'pilot_runs': len(rows), 'cost_cny': str(cost),
                      'completed': {c: totals[c]['task_completed'] for c in CONDITIONS}, 'layout_repairs': layout}, ensure_ascii=False))


if __name__ == '__main__':
    make()

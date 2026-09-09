"""Create a result report only after offline reconciliation of all 144 trials."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evidence/apparel_reliability_study_v1'
OUT = ROOT / 'research/APPAREL_RELIABILITY_RESULTS.md'
LABELS = {'single': '单 Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    if OUT.exists():
        raise FileExistsError('Preserve the first report; corrections require a new addendum')
    summary = read(DATA / 'summary.json')
    audit = read(ROOT / 'evidence/apparel_reliability_audit_20260909.json')
    assert audit['audit_completed'] and audit['business_and_ledger_reconciled']
    assert sum(g['runs'] for g in summary['groups'].values()) == 144
    groups = summary['groups']
    conclusion = '达到预登记的工作台接入门槛' if audit['engineering_gate_passed'] else '未达到预登记的工作台接入门槛'
    lines = ['# 服装跨境履约：提案与报告可靠性修复对照', '',
        f'2026-09-09。完成 24 个模拟状态 × 2 版本 × 3 策略，共 144 次首次尝试。修复版 v6 **{conclusion}**。', '',
        '这是运输提案复核的操作验收实验。商品检索 NDCG、首位精确命中率、模型选型表及原 216 次结果保持各自定义；不能将本轮验收率称为真实商家准确率。', '',
        '## 结果', '',
        '| 版本与策略 | 通过 / 尝试 | 验收率 | 平均费用（元/尝试） | 每个通过任务的成本（元，含失败） | 平均耗时（秒） | P95（秒） | 发生委派的任务 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for condition, g in groups.items():
        version, arm = condition.split('_', 1)
        cost_per_pass = str(round(Decimal(g['cost_cny']) / g['passed'], 5)) if g['passed'] else '不可计算'
        lines.append(f"| {version} · {LABELS[arm]} | {g['passed']}/{g['runs']} | {g['passed']/g['runs']:.2%} | {Decimal(g['mean_cost_cny']):.5f} | {cost_per_pass} | {g['mean_latency_seconds']:.2f} | {g['p95_latency_seconds']:.2f} | {g['tasks_with_delegation']}/{g['runs']} |")
    lines += ['', '费用及耗时的分母都是全部 24 次尝试，失败没有移除。每个通过任务的成本 = 该组全部费用 / 通过数，不是只取成功记录平均。P95 使用预登记的最近秩方法。', '',
        '| 版本与策略 | 模型调用成功 / 全部 | 工具成功 / 全部 | 输入 token | 输出 token | 总费用（元） | 专家委派次数 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for condition, g in groups.items():
        version, arm = condition.split('_', 1)
        lines.append(f"| {version} · {LABELS[arm]} | {g['successful_model_calls']}/{g['model_calls']} | {g['successful_tool_calls']}/{g['tool_calls']} | {g['input_tokens']:,} | {g['output_tokens']:,} | {Decimal(g['cost_cny']):.6f} | {g['delegations']} |")
    lines += ['', '模型调用成功只表示接口成功返回；达到调用上限、报告没交齐引用等仍可导致任务失败。耗时是整个 Agent 任务的端到端时间；没有单独测首 token、模型思考时长或纯解码时长。', '',
        '## 相同案例上的版本比较', '',
        '| 策略 | v6 赢 / 相同 / 输 | v6 总费用 / v5 | v6 平均耗时 / v5 |', '|---|---:|---:|---:|']
    for arm, label in LABELS.items():
        pair = summary['paired'][arm]; old, new = groups['v5_'+arm], groups['v6_'+arm]
        lines.append(f"| {label} | {pair['win']} / {pair['tie']} / {pair['loss']} | {Decimal(new['cost_cny'])/Decimal(old['cost_cny']):.3f} | {new['mean_latency_seconds']/old['mean_latency_seconds']:.3f} |")
    lines += ['', '赢/输指同一个状态的 v5/v6 操作验收是否由失败变通过、或由通过变失败。每组只有 24 个开发者编写状态、每条件一次随机轨迹；这些计数不构成统计显著性或总体泛化结论。', '',
        '## 门槛与程序解释', '', '| 预登记检查 | 结果 |', '|---|---|']
    gate_labels = {'minimum_each_arm': 'v6 各策略至少 23/24 通过', 'no_arm_acceptance_regression': '各策略通过数不低于对应 v5',
        'no_protected_violations': '原要求、批准和确认状态没有违规', 'total_cost_limit': 'v6 总费用不超过 v5 的 1.5 倍',
        'latency_limit_each_arm': '各策略平均耗时不超过对应 v5 的 1.5 倍', 'program_comparison_correct': '已完成提案复核的程序对照正确且覆盖完整'}
    for key, passed in audit['engineering_gate'].items():
        lines.append(f"| {gate_labels[key]} | {'通过' if passed else '未通过'} |")
    lines += ['', '| v6 策略 | 已完成提案复核 | 最终程序对照 | 对照检查错误 | 工具返回的对照检查数 |', '|---|---:|---:|---:|---:|']
    for arm, label in LABELS.items():
        g = audit['groups']['v6_'+arm]
        lines.append(f"| {label} | {g['program_expected']} | {g['program_comparisons']} | {g['program_errors']} | {g['program_tool_checks']} |")
    lines += ['', '程序对照独立检查原始 SQLite 中的新旧版本、班次身份、事件发布时间、取消与累计延误、实际起飞时间和等待变化，并核对固定中文解释模板。v5 没有该新增功能，缺少它不计入 v5 的业务失败。v6 的原始模型 rationale 仍保留且标为未验证；通过这项检查不证明所有自由文本正确。', '',
        '## 逐类结果', '', '| 案例类别（每条件 2 例） | v5 单 | v5 协调员 | v5 按需 | v6 单 | v6 协调员 | v6 按需 |', '|---|---:|---:|---:|---:|---:|---:|']
    for family in next(iter(groups.values()))['families']:
        cells = [f"{g['families'][family]['passed']}/2" for g in groups.values()]
        lines.append('| ' + family + ' | ' + ' | '.join(cells) + ' |')
    lines += ['', '## 全部未通过尝试', '']
    failed = [r for r in audit['cases'] if not r['passed']]
    if not failed:
        lines.append('本轮所有尝试通过所登记的操作检查；仍受上述数据、范围与自由文本限制。')
    for r in failed:
        rel = f"../evidence/apparel_reliability_study_v1/runs/{r['case_id']}-{r['condition']}/execution.json"
        lines.append(f"- [{r['case_id']} · {r['condition']}]({rel})：{r['run_status']}；未通过字段：{', '.join(r['failures'])}。")
    if any(r['case_id'] == 'RL-09-1' and r['condition'] == 'v5_coordinator' for r in failed):
        failure = read(DATA / 'runs/RL-09-1-v5_coordinator/execution.json')
        assert failure['model_calls'] == 12 and failure['delegations'] == 3
        reasons = {e['reason'] for t in failure['traces'] if t['kind'] == 'report_rejected' for e in t['invalid']}
        assert {'current_route_field_not_supported', 'current_order_status_not_supported'} <= reasons
        assert any(t['kind'] == 'tool_rejected' and 'not 13' in t.get('detail', '') for t in failure['traces'])
        lines += ['', '对 RL-09-1 旧版协调员轨迹的事后定位：先缺当前提案的费用与到达时间引用；补充时提交 13 条引用，超过既定 12 条上限；后续又缺当前订单状态依据。已发生 3 次委派，最终耗尽 12 次模型调用。原始失败保留。该反例支持继续研究证据整理，但本轮同时改变三项干预，不能单独归因于某一项提示。']
    lines += ['', '## 设计、来源与费用', '',
        'v5 是上一轮冻结的 SourceReviewAgent；v6 合并三项修复：只属于当前订单的对象目录、现有必需引用的精简建议、确定性运输修订对照。v6 不自动猜 ID、不代替模型读取材料、不增加调用上限；最终报告仍接受原状态与来源检查。三项同时改变，不是单因素消融。', '',
        '每次复制同一个初始 SQLite；模型请求固定 gpt-5.6-luna，保留服务实际返回模型名。12 次模型调用、32 次业务工具和 12 条最终引用的上限相同。6 个条件在每个执行位置各出现 4 次。新场景覆盖短/长延误、替代班次也延误、取消、未来/无关事件、下游等待、全部班次取消、二次修订、已批准替代、混合包装及只读订单。', '',
        '商品来自原 277 变体研究快照；库存、重量、价格、规则及 2028 年的班次事件均为明确模拟。不是新增客户或真实订单。路线核验只证明该单仓运输走廊中的可行性；无可行路线的案例另作空运班次完整取消和海运/铁路最短时长的独立论证，不主张最优或真实承运能力。', '',
        f"本轮 {audit['unique_paid_rows']} 条付费账本记录，{audit['paid_micro_cny']/1000000:.6f} 元；全项目 {audit['ledger_micro_cny']/1000000:.6f}/480 元。模型选型子任务仍为 64.986070/100 元，本轮没有选型调用。金额为本地保守 token 账本及未结预留，不是服务商发票。", '',
        f"请求模型：{', '.join(audit['requested_models'])}；返回标识：{', '.join(audit['returned_models'])}。", '',
        f"审计核对 {len(audit['verified_sha256'])} 个冻结文件散列，并单独记录运行后 SQLite/attempt 文件快照。全部首次轨迹保留，无挑选重跑、评分替换、真实用户、交易或发货。Finance-Agent 暂停。", '',
        '[预登记方案](APPAREL_RELIABILITY_PROTOCOL.md) · [汇总 JSON](../evidence/apparel_reliability_study_v1/summary.json) · [独立审计](../evidence/apparel_reliability_audit_20260909.json) · [完整登记与散列](../evidence/apparel_reliability_study_v1/registration.json) · [前轮 216 次与失败诊断](APPAREL_EXPANSION_RESULTS.md)', '',
        '本报告先记录已完成实验；工作台是否接入及接入后的交互检查另有实施回执，不从实验完成自动推断已经上线。', '']
    with OUT.open('x', encoding='utf-8') as file:
        file.write('\n'.join(lines))
    print(json.dumps({'report': str(OUT), 'engineering_gate_passed': audit['engineering_gate_passed'],
                      'failed_attempts': len(failed)}, ensure_ascii=False))


if __name__ == '__main__':
    main()

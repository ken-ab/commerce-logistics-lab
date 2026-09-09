"""Complete-pilot reporting; cannot alter execution, expectations or scores."""
from collections import Counter
import csv
from decimal import Decimal
import json

import numpy as np

from apparel_fulfillment.agent import ARMS
from apparel_fulfillment.data import ROOT
from research.apparel_analysis import aggregate
from research.apparel_experiment import save, sha
from research.apparel_state_pilot_v3 import DIRECTORY, CONDITIONS, validate_pilot

LABELS = {'single': '单Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}
CONFIGS = {'neither': '两项关闭', 'bootstrap': '仅起始状态', 'guard': '仅动作检查', 'both': '两项开启'}
METRICS = ('task_accuracy', 'avg_cost_cny', 'avg_latency_seconds')
WEIGHTS = {'bootstrap_effect': {'bootstrap': .5, 'neither': -.5, 'both': .5, 'guard': -.5},
           'guard_effect': {'guard': .5, 'neither': -.5, 'both': .5, 'bootstrap': -.5},
           'interaction': {'both': 1, 'bootstrap': -1, 'guard': -1, 'neither': 1},
           'both_minus_neither': {'both': 1, 'neither': -1}}


def failure_origin(row):
    if row['run_status'] == 'interrupted':
        return 'process_interruption'
    errors = [c.get('error_message', '') for c in row['calls']]
    if any('HTTP 403' in e and 'account balance is insufficient' in e for e in errors):
        return 'aihubmix_account_balance'
    if row['run_status'] != 'completed':
        return 'other_execution_failure'
    return 'completed_report_failure' if not row['score']['task_completed'] else 'completed_task'


def vector(row):
    return np.array([float(row['score']['task_completed']), float(row['accounted_and_reserved_cny']), row['latency_seconds']])


def factorial(rows, arms=ARMS):
    lookup = {(r['case_id'], r['condition'], r['arm']): r for r in rows}
    ids = sorted({r['case_id'] for r in rows})
    # Jointly resample whole scenarios, preserving all 12 paired configurations.
    timed_ids = [case for case in ids if all(r.get('latency_observation') != 'lower_bound_censored' for r in rows if r['case_id'] == case)]
    output = {}
    for effect, weights in WEIGHTS.items():
        values = np.array([np.mean([sum(weight * vector(lookup[(case, condition, arm)])
                                          for condition, weight in weights.items()) for arm in arms], axis=0) for case in ids])
        output[effect] = {}
        for j, name in enumerate(METRICS):
            retained = [i for i, case in enumerate(ids) if j != 2 or case in timed_ids]
            chosen = values[retained, j]
            draws = np.random.default_rng(26090832).integers(0, len(chosen), (5000, len(chosen)))
            samples = chosen[draws].mean(axis=1)
            output[effect][name] = {'difference': float(chosen.mean()), 'paired_scenarios': len(chosen),
                                    'paired_scenario_95_interval': np.percentile(samples, [2.5, 97.5]).tolist()}
    return {'scenarios': len(ids), 'complete_timing_scenarios': len(timed_ids), 'arms': list(arms), 'bootstrap_iterations': 5000,
            'notice': 'Exploratory paired scenario bootstrap; known small catalogue, no claim of broad generalization or multiplicity-adjusted significance.',
            'effects': output}


def extra(rows):
    a = aggregate(rows)
    timed = [r for r in rows if r.get('latency_observation') != 'lower_bound_censored']
    a.update(avg_latency_lower_bound_all_runs=a['avg_latency_seconds'], complete_timing_runs=len(timed),
             censored_timing_runs=len(rows)-len(timed), avg_latency_seconds=float(np.mean([r['latency_seconds'] for r in timed])),
             p95_latency_seconds=float(np.percentile([r['latency_seconds'] for r in timed], 95)))
    checks = [t for r in rows for t in r['traces'] if t['kind'] == 'operation_check']
    a.update(host_initial_reads=sum(r['host_initial_reads'] for r in rows),
             host_completion_checks=sum(r['host_completion_checks'] for r in rows),
             failed_operation_checks=sum(bool(t['errors']) for t in checks),
             operation_check_reasons=dict(Counter(e['reason'] for t in checks for e in t['errors'])),
             final_operation_check_passed=sum(bool(r.get('report') and r['report'].get('operation_check', {}).get('passed')) for r in rows),
             contract_repair_attempts=sum(t['kind'] == 'report_rejected' and any(e.get('field') == 'operation_contract' for e in t['invalid']) for r in rows for t in r['traces']),
             citation_repair_attempts=sum(t['kind'] == 'report_rejected' and any(e.get('field') != 'operation_contract' for e in t['invalid']) for r in rows for t in r['traces']),
             p50_latency_seconds=float(np.percentile([r['latency_seconds'] for r in timed], 50)),
             reasoning_tokens_known=sum((c.get('usage') or {}).get('reasoning_tokens', 0) or 0 for r in rows for c in r['calls']),
             reasoning_usage_missing_calls=sum((c.get('usage') or {}).get('reasoning_tokens') is None for r in rows for c in r['calls']),
             original_strict_completed=sum(r['original_score']['task_completed'] for r in rows),
             supplementary_completed=sum(r['supplementary_score']['task_completed'] for r in rows),
             runtime_completion_rate=sum(r['run_status'] == 'completed' for r in rows) / len(rows))
    a.update(failure_origins=dict(Counter(failure_origin(r) for r in rows)),
             completed_reports=sum(r['run_status'] == 'completed' for r in rows),
             account_refusal_runs=sum(failure_origin(r) == 'aihubmix_account_balance' for r in rows))
    return a


def paired_changes(rows):
    lookup = {(r['case_id'], r['condition'], r['arm']): r for r in rows}
    pairs = []
    for arm in ARMS:
        for condition in ('bootstrap', 'guard', 'both'):
            outcomes = Counter()
            changed = []
            for case in sorted({r['case_id'] for r in rows}):
                left, right = lookup[(case, 'neither', arm)], lookup[(case, condition, arm)]
                difference = int(right['score']['task_completed']) - int(left['score']['task_completed'])
                name = 'improved' if difference > 0 else 'regressed' if difference < 0 else 'tied'
                outcomes[name] += 1
                if difference: changed.append({'case_id': case, 'change': name,
                    'left_origin': failure_origin(left), 'right_origin': failure_origin(right),
                    'both_reports_completed': left['run_status'] == right['run_status'] == 'completed'})
            pairs.append({'arm': arm, 'condition': condition, 'outcomes': dict(outcomes), 'changed_cases': changed})
    return pairs


def load_complete():
    registration = validate_pilot()
    summary_path = DIRECTORY / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if summary['runs'] != registration['expected_runs'] or len(summary['source_results_sha256']) != registration['expected_runs']:
        raise ValueError('Only the complete registered pilot may be reported')
    rows = []
    for name, expected in sorted(summary['source_results_sha256'].items()):
        if sha(ROOT / name) != expected: raise ValueError('Recorded result changed: ' + name)
        rows.append(json.loads((ROOT / name).read_text(encoding='utf-8')))
    if {(r['case_id'], r['condition'], r['arm']) for r in rows} != {tuple(j) for j in registration['jobs']}:
        raise ValueError('Unexpected or missing configuration')
    for case in {r['case_id'] for r in rows}:
        if len({r['initial_view_digest'] for r in rows if r['case_id'] == case}) != 1:
            raise ValueError('Unequal paired initial state')
    return registration, summary, rows


def make():
    registration, summary, rows = load_complete()
    groups = {c: {a: extra([r for r in rows if r['condition'] == c and r['arm'] == a]) for a in ARMS} for c in CONDITIONS}
    totals = {c: extra([r for r in rows if r['condition'] == c]) for c in CONDITIONS}
    effects = {'all_arms': factorial(rows), **{arm: factorial(rows, (arm,)) for arm in ARMS}}
    pairs = paired_changes(rows)
    cost = sum((Decimal(r['accounted_and_reserved_cny']) for r in rows), Decimal(0))
    comparison = {'scope': 'development_only', 'runs': len(rows), 'groups': groups, 'totals': totals,
                  'factorial_effects': effects, 'paired_changes': pairs, 'cost_cny': str(cost),
                  'source_summary_sha256': sha(DIRECTORY / 'summary.json')}
    interrupted = [r for r in rows if r.get('latency_observation') == 'lower_bound_censored']
    excluded_cases = {r['case_id'] for r in interrupted}
    complete_cases = [r for r in rows if r['case_id'] not in excluded_cases]
    comparison['interruption'] = {'runs': len(interrupted), 'cases_excluded_only_from_paired_timing': sorted(excluded_cases),
        'complete_case_sensitivity': {c: extra([r for r in complete_cases if r['condition'] == c]) for c in CONDITIONS},
        'notice': 'All interrupted runs remain failures in the full task/cost analysis. Complete-case performance is supplementary only; latency uses available full observations and paired complete scenarios.'}
    comparison['service_failure'] = {'provider': 'aihubmix', 'model': 'gpt-5.6-luna', 'endpoint_host': 'aihubmix.com',
        'origins': dict(Counter(failure_origin(r) for r in rows)),
        'account_refusal_runs': sum(failure_origin(r) == 'aihubmix_account_balance' for r in rows),
        'notice': 'HTTP 403 account-balance failures clustered near the end. Full-denominator results are operational outcomes, not isolated algorithm effects; fast refusals also bias latency downward. No failed case was retried.'}
    save(DIRECTORY / 'comparison.json', comparison)
    flat = []
    for r in rows:
        s = r['score']
        row = {k: r[k] for k in ('case_id', 'scenario', 'family', 'condition', 'arm', 'run_status', 'model_calls',
                               'tool_calls', 'input_tokens', 'output_tokens', 'host_initial_reads', 'host_completion_checks',
                               'accounted_and_reserved_cny', 'latency_seconds', 'delegations')}
        row.update({k: s[k] for k in ('task_completed', 'business_success', 'required_evidence_covered', 'constraint_violation')})
        row.update(original_strict_completed=r['original_score']['task_completed'], supplementary_completed=r['supplementary_score']['task_completed'],
                   latency_observation=r.get('latency_observation', 'complete'), failure_origin=failure_origin(r),
                   final_operation_check_passed=bool(r.get('report') and r['report'].get('operation_check', {}).get('passed')),
                   failure_reasons=';'.join(s['failure_reasons']), evidence_gaps=';'.join(s['evidence_gaps']))
        flat.append(row)
    with (DIRECTORY / 'case_results.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flat[0])); writer.writeheader(); writer.writerows(flat)
    write_markdown(summary, comparison, rows)
    print(json.dumps({'runs': len(rows), 'cost_cny': str(cost), 'completion': {c: totals[c]['task_completed'] for c in CONDITIONS}}, ensure_ascii=False))


def write_markdown(summary, comparison, rows):
    groups, totals = comparison['groups'], comparison['totals']
    t = ['# 订单状态与动作检查：144次开发试跑', '',
         '12个开发场景×四种配置×三种策略全部记录。新增调用费用及预留共 **' + comparison['cost_cny'] + '元**。所有比较共用v3操作提示、逐行选择快照与字段目录；“两项关闭”并非历史v1。此轮不替换已验收工作台默认。', '',
         '**本轮不能确认算法提升。** 113次返回完成报告，其中111次通过任务评分；28次因AIHubMix账户余额不足返回HTTP 403，另3次进程中断。全部保留在144分母，未重试。两项开启29/36、关闭27/36受到服务故障影响，不能把差值解释为检查机制带来的收益。服务为aihubmix.com上的gpt-5.6-luna，与阿里云余额无关。', '',
         '| 配置 | 任务完成/36 | 业务正确/36 | 必要来源完整/36 | 约束违规 | 平均费用 CNY | 完整耗时均值 s | P95 s |',
         '|---|---:|---:|---:|---:|---:|---:|---:|']
    for c in CONDITIONS:
        a = totals[c]
        t.append(f'| {CONFIGS[c]} | {a["task_completed"]} | {round(a["business_success"]*36)} | {round(a["evidence_coverage"]*36)} | {a["constraint_violation_runs"]} | {a["avg_cost_cny"]:.5f} | {a["avg_latency_seconds"]:.2f} | {a["p95_latency_seconds"]:.2f} |')
    interrupted = comparison['interruption']
    if interrupted['runs']:
        t += ['', f'**执行中断：{interrupted["runs"]}次。** 原进程退出时100次已完成、3次正在执行、41次未开始。3次按未完成记录，保留持久轨迹及全额未知预留；仅续跑未开始的41次。终止原因未证实。此处耗时均值/P95仅用完整计时，各配置有效数依次为' + '/'.join(str(totals[c]['complete_timing_runs']) for c in CONDITIONS) + '。原summary的全量时延包含3条下界，不能当完整时延。详见[恢复与计时处理](APPAREL_STATE_V3_INTERRUPTION.md)。']
    t += ['', '**服务故障明细**：28次余额拒绝集中在2026-09-08 18:16:39–18:16:51（上海时间）。旧批量驱动没有在首次账户错误后停止排队任务，后续独立任务也遭到拒绝；这是运行控制缺陷，不是28次模型业务判断错误。费用保留28次不确定预留及3次中断预留。时延统计包含快速403，不能据此宣称响应更快。', '',
          '| 配置 | 完成报告/36 | 账户拒绝/36 | 进程中断/36 | 完成报告中的任务通过（仅条件描述） |',
          '|---|---:|---:|---:|---:|']
    for c in CONDITIONS:
        a = totals[c]
        t.append(f'| {CONFIGS[c]} | {a["completed_reports"]} | {a["account_refusal_runs"]} | {a["censored_timing_runs"]} | {a["task_completed"]}/{a["completed_reports"]} |')
    t += ['', '完成报告中的比例排除了基础设施失败，只能辅助诊断，不能替换主分母；仅1个场景全部12配置均返回报告，不能凭完整场景重建可靠对照。按需策略实际委派0次，本轮没有验证按需协作收益。', '',
          '每个配置含12个业务场景的三种策略，不能当作36个独立业务场景。任务完成同时要求正确业务状态及必要来源；模型返回输出和HTTP成功另计。所有失败都在分母。', '',
          '| 策略 | 配置 | 完成/12 | 原严格/补充完成 | 平均费用 CNY | 平均时延 s | 模型调用总数 | 委派任务数 |',
          '|---|---|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        for c in CONDITIONS:
            a = groups[c][arm]
            t.append(f'| {LABELS[arm]} | {CONFIGS[c]} | {a["task_completed"]} | {a["original_strict_completed"]}/{a["supplementary_completed"]} | {a["avg_cost_cny"]:.5f} | {a["avg_latency_seconds"]:.2f} | {a["model_calls"]} | {a["delegation_runs"]}/12 |')
    t += ['', '主开发指标在冻结语义口径上增加独立的逐行期望选择、实际选择和报告选择核对。保留前两份分数，不把运行时动作检查本身当作答案或评分器。新字段带来的标准变化已在调用前登记，因此不能直接拿本轮完成率与历史324次或54次相比。', '',
          '## 两个因素的配对分析', '',
          '下表保留预设描述分析；区间不消除集中发生的账户故障，不能用来作算法因果判断或晋级依据。', '',
          '| 因素 | 完成率差 pp（95%区间） | 每次费用差 CNY（95%区间） | 每次时延差 s（95%区间） |',
          '|---|---:|---:|---:|']
    names = {'bootstrap_effect': '起始读取主效应', 'guard_effect': '动作检查主效应', 'interaction': '两因素交互', 'both_minus_neither': '两项开启−两项关闭'}
    for effect, values in comparison['factorial_effects']['all_arms']['effects'].items():
        cells = []
        for metric, scale, digits in zip(METRICS, (100, 1, 1), (2, 5, 2)):
            value = values[metric]; lo, hi = value['paired_scenario_95_interval']
            cells.append(f'{value["difference"]*scale:+.{digits}f} [{lo*scale:+.{digits}f}, {hi*scale:+.{digits}f}]')
        t.append('| ' + names[effect] + ' | ' + ' | '.join(cells) + ' |')
    timed_cases = comparison['factorial_effects']['all_arms']['complete_timing_scenarios']
    t += ['', f'主效应对另一个开关和三种策略取平均；交互为差中之差。按完整场景联合重采样5000次，任务/费用保留12个场景，配对时延仅用全部配置计时完整的{timed_cases}个场景。区间仅作探索性描述，未作多比较校正。API运行顺序预先打乱、最多3并发，时延含工具、模型和修复；没有测到纯思考秒数。', '',
          '## 运行代价与检查', '',
          '| 指标（总数，除特别注明） | 两项关闭 | 仅起始状态 | 仅动作检查 | 两项开启 |', '|---|---:|---:|---:|---:|']
    metrics = [('输入token', 'input_tokens_known'), ('输出token', 'output_tokens_known'), ('可观测推理token', 'reasoning_tokens_known'),
               ('模型调用', 'model_calls'), ('起始host读取', 'host_initial_reads'), ('结束状态检查', 'host_completion_checks'),
               ('发现未满足操作要求的结束尝试', 'failed_operation_checks'), ('动作检查触发的修复', 'contract_repair_attempts'),
               ('字段引用触发的修复', 'citation_repair_attempts'), ('所有被拒绝报告尝试', 'report_repair_attempts'),
               ('最终报告通过完整操作检查', 'final_operation_check_passed'), ('无效工具尝试', 'rejected_tool_attempts')]
    for label, key in metrics: t.append('| ' + label + ' | ' + ' | '.join(str(totals[c][key]) for c in CONDITIONS) + ' |')
    t += ['', '检查关闭组也计算并留存诊断，但错误不返回模型、不会自动修改状态。一次拒绝可能同时含引用和动作问题，分类计数不能简单相加。推理token仅采用服务返回字段，缺失不等于0；详情见机器读数。执行回执来自程序，独立于模型引用，未补入模型证据得分。']
    if interrupted['runs']:
        t += ['', '执行中断分布不均，另列无进程中断场景敏感性分析（仍含HTTP 403，不取代全144分母）：', '']
        for c in CONDITIONS:
            a = interrupted['complete_case_sensitivity'][c]
            t.append(f'- {CONFIGS[c]}：{a["task_completed"]}/{a["scheduled"]}完成；仅含所有配置均无执行中断的场景。')
    t += ['',
          '## 全部未完成运行', '',
          '基础设施失败后的评分缺口是未完成输出的后果，不逐项归咎于模型能力。', '',
          '| 场景 | 策略 | 配置 | 失败来源 | 业务评分缺口 | 证据缺口 |', '|---|---|---|---|---|---|']
    failures = [r for r in rows if not r['score']['task_completed']]
    for r in failures:
        s = r['score']
        origin = {'process_interruption': '进程中断', 'aihubmix_account_balance': '账户403', 'completed_report_failure': '完成报告缺口'}.get(failure_origin(r), failure_origin(r))
        t.append(f'| {r["scenario"]} | {LABELS[r["arm"]]} | {CONFIGS[r["condition"]]} | {origin} | {"; ".join(s["failure_reasons"]) or "无"} | {"; ".join(s["evidence_gaps"]) or "无"} |')
    if not failures: t.append('| 本轮未观察到 | — | — | — | — | — |')
    t += ['', '## 适用范围和证据', '',
          '28项免费定向测试及12个免费环境预检查通过后才登记付费试跑。开发期的业务场景、数据、提示、评分与运行代码在144次调用期间冻结；结果不重试、不挑选。已批准替代、无关事件、多行错配均在这次场景中。', '',
          '自由理由没有逐句事实判定；动作检查不能证明候选全局最优，研究侧另做场景期望核验。商品来源公开可追溯，库存、规则、运输和业务任务明确模拟。真实客户/用户仍为0，未开展商家上线实验。需另有未用案例和界面验收才能决定晋级。', '',
          f'完成时全项目本地费用核算与预留 **{summary["ledger_after"]["accounted_and_reserved_cny"]}元**，上限480元。不是供应商账单；100模型选型未重跑，Finance-Agent保持暂停。', '',
          '- [试跑协议](APPAREL_STATE_PILOT_V3_PROTOCOL.md)',
          '- [144行逐例CSV](../evidence/apparel_state_pilot_v3/case_results.csv)',
          '- [四条件统计与配对区间](../evidence/apparel_state_pilot_v3/comparison.json)',
          '- [登记的144条原始文件SHA](../evidence/apparel_state_pilot_v3/summary.json)',
          '- [设计决策](../docs/adr/0003-apparel-action-contract.md)', '']
    t += ['- [失败与修复轨迹复核](APPAREL_STATE_PILOT_V3_FAILURE_REVIEW.md)', '']
    (ROOT / 'research/APPAREL_STATE_PILOT_V3_RESULTS.md').write_text('\n'.join(t), encoding='utf-8')


if __name__ == '__main__': make()

"""Predeclared paired summaries. No feedback is sent to agents or test inputs."""
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import json
from pathlib import Path

import numpy as np

from apparel_fulfillment.agent import ARMS
from apparel_fulfillment.data import ROOT


def aggregate(rows):
    n = len(rows)
    costs = [float(r['accounted_and_reserved_cny']) for r in rows]
    latency = [r['latency_seconds'] for r in rows]
    model_calls = sum(r['model_calls'] for r in rows)
    tool_calls = sum(r['tool_calls'] for r in rows)
    return {'scheduled': n, 'task_completed': sum(r['score']['task_completed'] for r in rows),
            'task_accuracy': np.mean([r['score']['task_completed'] for r in rows]).item(),
            'business_success': np.mean([r['score']['business_success'] for r in rows]).item(),
            'evidence_coverage': np.mean([r['score']['required_evidence_covered'] for r in rows]).item(),
            'constraint_violation_runs': sum(r['score']['constraint_violation'] for r in rows),
            'avg_cost_cny': float(np.mean(costs)), 'total_accounted_and_reserved_cny': str(sum((Decimal(r['accounted_and_reserved_cny']) for r in rows), Decimal(0))),
            'total_settled_cost_cny': str(sum((Decimal(r['settled_cost_cny']) for r in rows), Decimal(0))),
            'avg_latency_seconds': float(np.mean(latency)), 'p95_latency_seconds': float(np.percentile(latency, 95)),
            'avg_model_calls': model_calls / n, 'model_calls': model_calls,
            'model_call_success': sum(r['successful_model_calls'] for r in rows) / model_calls if model_calls else None,
            'avg_tool_calls': tool_calls / n,
            'tool_call_success': sum(r['successful_tool_calls'] for r in rows) / tool_calls if tool_calls else None,
            'input_tokens_known': sum(r['input_tokens'] for r in rows), 'output_tokens_known': sum(r['output_tokens'] for r in rows),
            'calls_without_returned_usage': sum(c.get('usage') is None for r in rows for c in r['calls']),
            'delegation_runs': sum(r['delegations'] > 0 for r in rows), 'avg_delegations': np.mean([r['delegations'] for r in rows]).item(),
            'report_repair_attempts': sum(r['score']['report_repair_attempts'] for r in rows),
            'rejected_tool_attempts': sum(r['score']['rejected_tool_attempts'] for r in rows),
            'run_status_counts': dict(Counter(r['run_status'] for r in rows)),
            'returned_models': dict(Counter(c.get('returned_model') for r in rows for c in r['calls'] if c.get('returned_model'))),
            'failure_reasons': dict(Counter(reason for r in rows for reason in r['score']['failure_reasons'])),
            'evidence_gaps': dict(Counter(reason for r in rows for reason in r['score']['evidence_gaps']))}


def paired(left, right, *, iterations=10000, seed=260908):
    a, b = {r['case_id']: r for r in left}, {r['case_id']: r for r in right}
    if set(a) != set(b): raise ValueError('Paired cases differ')
    ids = sorted(a)
    values = np.array([[float(a[k]['score']['task_completed']) - float(b[k]['score']['task_completed']),
                        float(a[k]['accounted_and_reserved_cny']) - float(b[k]['accounted_and_reserved_cny']),
                        a[k]['latency_seconds'] - b[k]['latency_seconds']] for k in ids])
    rng = np.random.default_rng(seed)
    samples = values[rng.integers(0, len(ids), size=(iterations, len(ids)))].mean(axis=1)
    groups = defaultdict(list)
    for i, k in enumerate(ids): groups[a[k]['target_sku']].append(i)
    groups = list(groups.values())
    cluster_sums = np.array([values[g].sum(axis=0) for g in groups])
    cluster_counts = np.array([len(g) for g in groups])
    draws = rng.integers(0, len(groups), size=(iterations, len(groups)))
    cluster_samples = cluster_sums[draws].sum(axis=1) / cluster_counts[draws].sum(axis=1)[:, None]
    return {name: {'difference': float(values[:, j].mean()),
                   'paired_case_95_interval': [float(x) for x in np.percentile(samples[:, j], [2.5, 97.5])],
                   'target_sku_cluster_95_interval': [float(x) for x in np.percentile(cluster_samples[:, j], [2.5, 97.5])],
                   'target_sku_clusters': len(groups)}
            for j, name in enumerate(('task_accuracy', 'avg_cost_cny', 'avg_latency_seconds'))}


def analyze(partition):
    from research.apparel_experiment import DIRECTORY, validate, save, sha
    method = validate()
    data = json.loads((ROOT / f'data/apparel_cases_{partition}_v1.json').read_text(encoding='utf-8'))
    rows, by_arm = [], {}
    for arm in ARMS:
        by_arm[arm] = []
        for case in data['cases']:
            path = DIRECTORY / partition / case['id'] / arm / 'result.json'
            row = json.loads(path.read_text(encoding='utf-8'))
            row = {key: value for key, value in row.items() if key not in {'observations', 'traces', 'before', 'after', 'report'}}
            if row['case_id'] != case['id'] or row['arm'] != arm or row['method_sha256'] != sha(DIRECTORY / 'method.json'):
                raise ValueError('Result identity/method mismatch')
            by_arm[arm].append(row); rows.append(row)
    for case in data['cases']:
        digests = {r['initial_state_digest'] for r in rows if r['case_id'] == case['id']}
        if len(digests) != 1: raise ValueError('Unequal initial states')
    summaries = {arm: aggregate(arm_rows) for arm, arm_rows in by_arm.items()}
    comparisons = {arm + '_minus_single': paired(by_arm[arm], by_arm['single'], iterations=method['bootstrap_iterations'], seed=method['bootstrap_seed'])
                   for arm in ('coordinator', 'on_demand')}
    family = {name: {arm: aggregate([r for r in arm_rows if r['family'] == name]) for arm, arm_rows in by_arm.items()} for name in data['families']}
    output = {'status': 'complete', 'partition': partition, 'cases_per_arm': len(data['cases']), 'runs': len(rows),
              'method_sha256': sha(DIRECTORY / 'method.json'), 'arms': summaries, 'families': family,
              'paired_differences': comparisons,
              'metric_notice': 'Primary Task Accuracy includes required evidence coverage. Not retrieval NDCG, live merchant success or a free-text factuality judge.',
              'latency_notice': 'Same maximum concurrency of three; wall time includes orchestration and source repairs, not fixture setup. TTFT/thinking seconds not measured.',
              'cost_notice': 'Accounted token estimates plus full unknown reservations, not supplier invoices.'}
    folder = DIRECTORY / partition
    save(folder / 'summary.json', output)
    if partition == 'validation':
        safe = {arm: s for arm, s in summaries.items() if s['constraint_violation_runs'] == 0}
        top = max((s['task_accuracy'] for s in safe.values()), default=0)
        eligible = [arm for arm, s in safe.items() if s['task_accuracy'] >= top - .02]
        selected = min(eligible, key=lambda arm: (summaries[arm]['avg_cost_cny'], summaries[arm]['avg_latency_seconds'])) if eligible else None
        save(folder / 'selection.json', {'selected_arm': selected, 'eligible': eligible, 'method_sha256': output['method_sha256'],
             'rule': 'No constraint violations; within 2 percentage points of best validation Task Accuracy, then lowest cost and latency. Test not used.'})
    flat = []
    for row in rows:
        flat.append({k: row[k] for k in ('case_id', 'family', 'target_sku', 'arm', 'run_status', 'model_calls', 'tool_calls', 'input_tokens', 'output_tokens',
                                      'accounted_and_reserved_cny', 'latency_seconds', 'delegations')} |
                    {k: row['score'][k] for k in ('task_completed', 'business_success', 'required_evidence_covered', 'constraint_violation')} |
                    {'failure_reasons': '|'.join(row['score']['failure_reasons']), 'evidence_gaps': '|'.join(row['score']['evidence_gaps'])})
    with (folder / 'case_results.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flat[0])); writer.writeheader(); writer.writerows(flat)
    labels = {'single': '单 Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}
    text = [f'# 服装订单与跨境履约 · {"验证" if partition == "validation" else "最终测试"}结果', '',
            f'{len(data["cases"])} 个模拟任务 × 3 种策略，共 {len(rows)} 次登记运行。所有失败保留分母，初始状态逐例核对一致。', '',
            '| 策略 | 任务完成 | 业务成功 | 证据完整 | 约束违规 | 平均费用 CNY | 平均时延 s | P95 s | 平均模型调用 | 委派任务 |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for arm, s in summaries.items():
        text.append(f'| {labels[arm]} | {s["task_completed"]}/{s["scheduled"]} ({s["task_accuracy"]:.2%}) | {s["business_success"]:.2%} | {s["evidence_coverage"]:.2%} | {s["constraint_violation_runs"]} | {s["avg_cost_cny"]:.5f} | {s["avg_latency_seconds"]:.2f} | {s["p95_latency_seconds"]:.2f} | {s["avg_model_calls"]:.2f} | {s["delegation_runs"]} |')
    text += ['', '任务完成同时要求业务正确和指定来源完整。最终展示字段由程序从工具返回中取值；此处不把自由理由中的每句话都视为已校验事实。', '', '## 分业务类型', '',
             '| 类型 | 单 Agent | 协调员加专家 | 按需委派 |', '|---|---:|---:|---:|']
    for name, arms in family.items():
        text.append('| ' + name + ' | ' + ' | '.join(f'{arms[a]["task_completed"]}/{arms[a]["scheduled"]}; ¥{arms[a]["avg_cost_cny"]:.4f}; {arms[a]["avg_latency_seconds"]:.1f}s' for a in ARMS) + ' |')
    text += ['', '## 与单 Agent 配对的差异', '', '正的完成率差值有利于比较组；正的费用或时延差值表示代价更高。下列区间按目标 SKU 聚类重采样，仅反映本模拟集合。', '',
             '| 比较组减单 Agent | 完成率差及 95% 区间 | 平均费用差 CNY | 平均时延差 s |', '|---|---:|---:|---:|']
    for arm in ('coordinator', 'on_demand'):
        d = comparisons[arm + '_minus_single']; q = d['task_accuracy']; ci = q['target_sku_cluster_95_interval']
        text.append(f'| {labels[arm]} | {q["difference"]*100:+.2f} pp [{ci[0]*100:+.2f}, {ci[1]*100:+.2f}] | {d["avg_cost_cny"]["difference"]:+.5f} | {d["avg_latency_seconds"]["difference"]:+.2f} |')
    text += ['', '## 失败与调用记录', '']
    for arm, s in summaries.items():
        text += [f'### {labels[arm]}', '', f'调用成功率 {s["model_call_success"]:.2%}；已知输入/输出 token {s["input_tokens_known"]:,}/{s["output_tokens_known"]:,}；未返回 usage 的调用 {s["calls_without_returned_usage"]}。引用修正 {s["report_repair_attempts"]} 次；被拒工具尝试 {s["rejected_tool_attempts"]} 次。', '',
                 '业务失败：' + (json.dumps(s['failure_reasons'], ensure_ascii=False) if s['failure_reasons'] else '无'), '',
                 '来源缺项：' + (json.dumps(s['evidence_gaps'], ensure_ascii=False) if s['evidence_gaps'] else '无'), '']
    text += ['## 可复核文件与解释范围', '', '[逐例结果 CSV](case_results.csv)；同目录每个 case ID/策略包含原始 `result.json`、初始数据库副本和模型/工具轨迹。`summary.json` 另含逐组费用、时延及全部配对区间。', '',
             '商品文字来自 Amazon ESCI；款式分组及商家/运输条件为模拟。测试目标 SKU 与验证目标不重合，但目录可共同搜索。案例复用模板且共享目录，不能外推实际商家成功率。', '',
             '模型 ID/参数固定，不代表上游服务保证不可变权重快照。时延包含网络与服务排队；未测首 token 或真实思考秒数。费用为保守账本估计和预留，非平台发票。', '',
             '[登记方案](../../../research/APPAREL_EXPERIMENT_PROTOCOL.md) 说明指标、数据分区、冻结与选择规则。']
    (folder / 'readout.md').write_text('\n'.join(text) + '\n', encoding='utf-8')
    print(json.dumps({'partition': partition, 'runs': len(rows), 'arms': {a: {k: s[k] for k in ('task_accuracy', 'avg_cost_cny', 'avg_latency_seconds')} for a, s in summaries.items()}}, ensure_ascii=False))
    return output

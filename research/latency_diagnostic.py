"""Read-only, post-hoc latency diagnostics from the completed paired validation.

No model imports, network requests, score changes, or budget mutations.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import fmean


ROOT = Path(__file__).resolve().parents[1]
READOUT = Path('evidence/model_selection_100/budget100_v2/validation_readout.json')
READOUT_SHA = 'daf559347d916358de6cbc845f0457108acb1891342d9e32c53d027c114a6b6e'
DEADLINES = (2, 5, 10)


def quantile(values, probability):
    """Linear interpolation at (n-1)*p, also for singleton samples."""
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * probability
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def describe(values):
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError('Invalid timing observation')
    return {'observed': len(values), 'mean': fmean(values) if values else None,
            **{f'p{int(p*100)}': quantile(values, p) for p in (.5, .9, .95, .99)},
            'max': max(values) if values else None}


def summarize(rows):
    if not rows:
        raise ValueError('Empty diagnostic cohort')
    if any(not r['submitted'] for r in rows):
        raise ValueError('Unsubmitted calls cannot become latency observations')
    n = len(rows)
    latency = [r['response']['latency_seconds'] for r in rows]
    valid = [r for r in rows if r['valid_response']]
    total_cost = sum((Decimal(r['response']['accounted_and_reserved_cny']) for r in rows), Decimal(0))
    fields = ('ttft_seconds', 'time_to_first_answer_seconds', 'answer_stream_span_seconds',
              'observed_reasoning_span_seconds', 'pure_thinking_seconds')
    timing = {key: describe([r['response'].get('timing', {}).get(key) for r in rows
                            if r['response'].get('timing', {}).get(key) is not None]) for key in fields}
    deadline_rows = []
    for seconds in DEADLINES:
        eligible = [r for r in valid if r['response']['latency_seconds'] <= seconds]
        exact = sum(r['metrics']['hit_exact_at_1'] for r in eligible)
        deadline_rows.append({'deadline_seconds': seconds, 'total_requests': n,
            'valid_within_deadline': len(eligible), 'valid_within_deadline_rate': len(eligible)/n,
            'exact_top1_within_deadline': exact, 'exact_top1_within_deadline_rate': exact/n,
            'cost_per_valid_within_deadline_cny': str(total_cost/len(eligible)) if eligible else None})
    return {'requests': n, 'valid': len(valid), 'failures': n-len(valid),
        'failure_reasons': dict(Counter(r['status'] for r in rows if not r['valid_response'])),
        'latency_all_requests_seconds': describe(latency),
        'latency_valid_only_seconds': describe([r['response']['latency_seconds'] for r in valid]),
        'timing': timing, 'deadlines': deadline_rows,
        'accounted_and_reserved_cny': str(total_cost),
        'observed_reasoning_chunks_calls': sum(r['response'].get('timing', {}).get('observed_reasoning_chunks', 0) > 0 for r in rows),
        'positive_reasoning_tokens_calls': sum(r['response'].get('usage', {}).get('reasoning_tokens', 0) > 0 for r in rows),
        'multiple_content_chunks_zero_span_calls': sum(
            r['response'].get('timing', {}).get('observed_content_chunks', 0) >= 2 and
            r['response'].get('timing', {}).get('answer_stream_span_seconds') == 0 for r in rows)}


def analyze(root=ROOT):
    raw_readout = (root/READOUT).read_bytes()
    if hashlib.sha256(raw_readout).hexdigest() != READOUT_SHA:
        raise ValueError('Completed validation readout changed')
    evidence = json.loads(raw_readout)
    models = (evidence['winner'], evidence['reference'])
    cohorts = {model: [] for model in models}
    keys = {model: set() for model in models}
    ids = set()
    with sqlite3.connect((root/'evidence/api_budget.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        for relative, expected_sha in evidence['raw_result_sha256'].items():
            path = (root/relative).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError('Evidence path escaped project')
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected_sha:
                raise ValueError(f'Validation evidence changed: {relative}')
            row = json.loads(payload)
            model = row['model_id']
            if row['stage'] != 'validation' or model not in cohorts:
                raise ValueError('Unexpected validation cohort')
            key = (row['locale'], str(row['query_id']))
            call_id = row['response']['budget_call_id']
            if key in keys[model] or call_id in ids:
                raise ValueError('Duplicate query or charged call')
            keys[model].add(key)
            ids.add(call_id)
            ledger = db.execute('SELECT model,purpose,COALESCE(charged,reserved) FROM calls WHERE id=?', (call_id,)).fetchone()
            if not ledger or ledger[0] != model or not ledger[1].startswith('model-selection-100:'):
                raise ValueError('Ledger identity mismatch')
            if Decimal(ledger[2])/1_000_000 != Decimal(row['response']['accounted_and_reserved_cny']):
                raise ValueError('Ledger accounting mismatch')
            cohorts[model].append(row)
    if len(ids) != 300 or any(len(keys[m]) != 150 for m in models) or keys[models[0]] != keys[models[1]]:
        raise ValueError('Expected the complete paired 150-query validation')
    result = {model: summarize(cohorts[model]) for model in models}
    for model in models:
        baseline = evidence['summaries'][model]
        if result[model]['valid'] != baseline['valid_responses'] or not math.isclose(
                result[model]['latency_all_requests_seconds']['mean'], baseline['avg_latency_seconds'], abs_tol=1e-10):
            raise ValueError('Diagnostic does not reconcile to the original readout')
    return {'created_at': datetime.now(timezone.utc).isoformat(),
        'analysis_type': 'post_hoc_descriptive_no_selection_or_score_change',
        'source_readout': str(READOUT), 'source_readout_sha256': READOUT_SHA,
        'raw_files_verified': len(ids), 'ledger_calls_verified_read_only': len(ids),
        'paired_query_groups': 150, 'models': result,
        'deadline_rule': 'Illustrative sensitivity at 2/5/10 seconds; API request completion, inclusive. Not a pre-registered SLO or a new deployment gate.',
        'limits': [
            'Client-side HTTP duration includes network and provider queueing; their components are not identifiable.',
            'An SSE chunk can contain zero, one, or several tokens. Client chunk spans are not per-token engine decode time.',
            'No provider reasoning fragments does not mean no internal reasoning.',
            'No load/rate sweep was conducted; these observations do not establish service capacity or goodput.',
            'Local catalog retrieval, GPU preranking, report generation and UI rendering are not timed here.',
            'No new API requests or budget changes; the previously selected model and scores remain unchanged.']}


def report(result):
    models = result['models']
    lines = ['# 商品精排延迟与可观测性补充分析', '',
        '2026-09-08。复用已完成的150个共同验证查询、300次原始调用；逐文件哈希与预算记录均核对。'
        '本分析是事后描述，不改变模型选型、原综合分、失败记录或预登记结论。新增API费用为0。', '',
        '| 模型 | 有效回答 | 平均API秒数 | P50 | P90 | P95 | P99 | 最大秒数 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, data in models.items():
        lat = data['latency_all_requests_seconds']
        lines.append(f"| {name} | {data['valid']}/{data['requests']} | " + ' | '.join(f'{lat[k]:.3f}' for k in ('mean','p50','p90','p95','p99','max'))+' |')
    lines += ['', '主表包含所有请求的等待时间。分位数采用排序后的线性插值；P99受极少量慢请求影响，150题不足以证明长期尾延迟稳定。', '',
        '| 模型 | 完成时限（秒） | 时限内有效排列 | 时限内首位精确命中 | 每个限时有效排列的费用（CNY） |',
        '|---|---:|---:|---:|---:|']
    for name, data in models.items():
        for row in data['deadlines']:
            cost = row['cost_per_valid_within_deadline_cny']
            formatted = f'{float(cost):.6f}' if cost is not None else 'NA'
            lines.append(f"| {name} | {row['deadline_seconds']} | {row['valid_within_deadline']}/150 ({row['valid_within_deadline_rate']:.2%}) | "
                         f"{row['exact_top1_within_deadline']:.0f}/150 ({row['exact_top1_within_deadline_rate']:.2%}) | {formatted} |")
    lines += ['', '2/5/10秒是展示时限敏感性的例子，不是提前登记的SLA。分母始终为150：失败和超时限回答不计作限时成功。'
        '费用分子包含该模型全部150次调用的费用估计及未知预留；不把慢请求的费用删除。此表不能用于声称并发承载量或线上有效吞吐量。', '',
        '| 模型 | 首答案时间均值/观测数 | 回答流跨度均值/观测数 | 返回过正推理token数的请求 | 收到思考片段的请求 | 多片段但跨度为0的请求 |',
        '|---|---:|---:|---:|---:|---:|']
    for name, data in models.items():
        first = data['timing']['time_to_first_answer_seconds']
        span = data['timing']['answer_stream_span_seconds']
        show = lambda d: f"{d['mean']:.4f}s / {d['observed']}" if d['mean'] is not None else 'NA / 0'
        lines.append(f"| {name} | {show(first)} | {show(span)} | {data['positive_reasoning_tokens_calls']} | "
                     f"{data['observed_reasoning_chunks_calls']} | {data['multiple_content_chunks_zero_span_calls']} |")
    lines += ['', '首答案时间从发送请求到收到首个非空答案片段。回答流跨度是首末答案片段的客户端到达时间差，至少两片段才记数；'
        '同一时钟刻度收到多片段可能记为0，不意味着模型瞬间完成计算。usage中的推理token与是否输出思考片段是两种观测。'
        '纯思考时长、供应商排队时间、引擎Prefill/Decode时间均保持未知，不从这些字段反推。', '',
        '方法解释参考[vLLM指标文档](https://docs.vllm.ai/en/v0.10.2/design/metrics.html)对排队、首token及逐token时延的区分；'
        '[DistServe原论文](https://arxiv.org/html/2401.09670v1)讨论在指定负载和时延要求下的有效吞吐量。本次API比较未实施对应的负载扫描。', '',
        '本地0.6B预排使用一次前向计算、`use_cache=False`和末位置logits，逐字Decode优化不直接适用。'
        '浏览器全链路还包括目录检索、本地GPU预排、可选API精排及页面渲染，本表不能替代该链路的验收。', '',
        f"证据：`{READOUT.as_posix()}`，SHA-256 `{READOUT_SHA}`；机器可读补充为`evidence/model_latency_diagnostic_20260908.json`。", '']
    return '\n'.join(lines)


def main():
    result = analyze()
    payload = json.dumps(result, ensure_ascii=False, indent=2)+'\n'
    (ROOT/'evidence/model_latency_diagnostic_20260908.json').write_text(payload, encoding='utf-8')
    (ROOT/'research/MODEL_LATENCY_DIAGNOSTIC_20260908.md').write_text(report(result), encoding='utf-8')
    print(json.dumps({'raw_files_verified': result['raw_files_verified'], 'models': result['models']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

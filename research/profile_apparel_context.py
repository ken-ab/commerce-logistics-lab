"""Post-hoc, read-only profiling of frozen reliability-study API records.

No model imports, token estimation, prompt editing or score replacement. The
provider's token counters include API formatting/tools; message character counts
describe only the saved messages and must never be presented as token counts.
"""
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / 'evidence/apparel_reliability_study_v1'
OUT = ROOT / 'evidence/apparel_context_profile_20260909.json'
CSV = ROOT / 'evidence/apparel_context_calls_20260909.csv'
REPORT = ROOT / 'research/APPAREL_CONTEXT_PROFILE.md'
LABELS = {'single': '单 Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def compact(value, *, sorted_keys=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sorted_keys, separators=(',', ':'))


def micro(value):
    return int((Decimal(str(value)) * 1000000).to_integral_value(rounding=ROUND_CEILING))


def distribution(values):
    ordered = sorted(values)
    assert ordered
    return {'count': len(ordered), 'sum': sum(ordered), 'min': ordered[0],
            'median': statistics.median(ordered), 'mean': statistics.mean(ordered),
            'p95_nearest_rank': ordered[math.ceil(.95 * len(ordered)) - 1], 'max': ordered[-1]}


def summarize(rows):
    assert rows
    return {'calls': len(rows), 'input_tokens': distribution([r['input_tokens'] for r in rows]),
            'output_tokens': distribution([r['output_tokens'] for r in rows]),
            'reasoning_tokens_reported': distribution([r['reasoning_tokens'] for r in rows]),
            'message_characters': distribution([r['message_characters'] for r in rows]),
            'api_latency_seconds': distribution([r['api_latency_seconds'] for r in rows]),
            'cost_micro_cny': sum(r['cost_micro_cny'] for r in rows),
            'calls_above_input_threshold': {str(n): sum(r['input_tokens'] > n for r in rows)
                                          for n in (8192, 16384, 32768, 65536)},
            'roles': dict(Counter(r['role'] for r in rows)),
            'initial_context_calls': sum(r['initial_context'] for r in rows)}


def main():
    if any(p.exists() for p in (OUT, CSV, REPORT)):
        raise FileExistsError('Preserve the first completed profile; use a separately named addendum')
    previous = read(ROOT / 'knowledge/reading_review_20260909_reliability.json')
    assert len(previous['knowledge_file_sha256']) == 172
    for group in ('knowledge_file_sha256', 'frozen_sha256'):
        assert all(sha(ROOT / name) == value for name, value in previous[group].items())
    audit_path = ROOT / 'evidence/apparel_reliability_audit_20260909.json'
    audit = read(audit_path)
    assert audit['audit_completed'] and audit['business_and_ledger_reconciled']
    verified = {**audit['verified_sha256'], **audit['post_audit_snapshot_sha256']}
    assert all(sha(ROOT / n) == h for n, h in verified.items())
    summary = read(STUDY / 'summary.json')
    rows, run_rows, ids, consumed = [], [], set(), {audit_path.relative_to(ROOT).as_posix(): sha(audit_path)}
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        before = tuple(db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone())
        assert before == (339751400, 18928)
        for case in sorted(audit['cases'], key=lambda r: (r['condition'], r['case_id'])):
            condition, case_id = case['condition'], case['case_id']
            path = STUDY / f'runs/{case_id}-{condition}/execution.json'
            rel = path.relative_to(ROOT).as_posix()
            assert rel in verified and sha(path) == verified[rel]
            consumed[rel] = verified[rel]
            result = read(path)
            traces = sorted((t for t in result['traces'] if t['kind'] == 'model'), key=lambda t: t['call_number'])
            assert len(traces) == result['model_calls'] == len(result['calls'])
            assert result['successful_model_calls'] == result['model_calls']
            current = []
            for number, (call, trace) in enumerate(zip(result['calls'], traces), 1):
                assert trace['call_number'] == number
                assert {k: trace['response'][k] for k in call} == call
                messages = trace['messages']
                assert hashlib.sha256(compact(messages, sorted_keys=True).encode()).hexdigest() == trace['input_sha256']
                assert len(compact(messages)) == trace['input_characters']
                assert call['status'] == 'success' and call['role'] == trace['role']
                ident = call['budget_call_id']
                assert ident not in ids
                ids.add(ident)
                ledger = db.execute('SELECT * FROM calls WHERE id=?', (ident,)).fetchone()
                assert ledger is not None and ledger['status'] == 'settled'
                assert json.loads(ledger['usage']) == call['usage']
                assert ledger['charged'] == micro(call['estimated_cost_cny'])
                assert ledger['model'] == result['model'] == call['requested_model']
                usage = call['usage']
                assert {'prompt_tokens', 'completion_tokens', 'reasoning_tokens'} <= usage.keys()
                assert all(isinstance(usage[k], int) and usage[k] >= 0 for k in usage)
                assert usage['reasoning_tokens'] <= usage['completion_tokens']
                initial = len(messages) == 2 and [m['role'] for m in messages] == ['system', 'user']
                row = {'case_id': case_id, 'condition': condition, 'case_passed': case['passed'],
                       'call_number': number, 'role': call['role'], 'root_role': call['role'] == case['arm'],
                       'initial_context': initial, 'input_tokens': usage['prompt_tokens'],
                       'output_tokens': usage['completion_tokens'], 'reasoning_tokens': usage['reasoning_tokens'],
                       'message_characters': trace['input_characters'], 'message_count': len(messages),
                       'tool_messages': sum(m['role'] == 'tool' for m in messages),
                       'api_latency_seconds': call['latency_seconds'], 'cost_micro_cny': ledger['charged'],
                       'budget_call_id': ident, 'input_sha256': trace['input_sha256'],
                       'execution_path': rel, 'execution_sha256': verified[rel]}
                assert math.isfinite(row['api_latency_seconds']) and row['api_latency_seconds'] >= 0
                current.append(row); rows.append(row)
            assert sum(r['input_tokens'] for r in current) == result['input_tokens']
            assert sum(r['output_tokens'] for r in current) == result['output_tokens']
            assert sum(r['cost_micro_cny'] for r in current) == micro(result['accounted_and_reserved_cny'])
            assert sum(r['initial_context'] and r['root_role'] for r in current) == 1
            run_rows.append({'case_id': case_id, 'condition': condition, 'passed': case['passed'],
                             'max_input_tokens': max(r['input_tokens'] for r in current),
                             'latency_seconds': result['latency_seconds'], 'calls': len(current)})
        expected_ids = {r[0] for r in db.execute('SELECT id FROM calls WHERE purpose LIKE ?', ('commerce_apparel:reliability_v1_%',))}
        assert ids == expected_ids and len(rows) == 745 and len(run_rows) == 144
        after = tuple(db.execute('SELECT SUM(COALESCE(charged,reserved)),COUNT(*) FROM calls').fetchone())
        assert before == after
    groups = {}
    for condition, reference in summary['groups'].items():
        subset = [r for r in rows if r['condition'] == condition]
        runs = [r for r in run_rows if r['condition'] == condition]
        g = summarize(subset)
        assert g['calls'] == reference['model_calls']
        assert g['input_tokens']['sum'] == reference['input_tokens']
        assert g['output_tokens']['sum'] == reference['output_tokens']
        assert g['cost_micro_cny'] == micro(reference['cost_cny'])
        assert len(runs) == reference['runs'] == 24 and sum(r['passed'] for r in runs) == reference['passed']
        assert math.isclose(statistics.mean(r['latency_seconds'] for r in runs), reference['mean_latency_seconds'], abs_tol=1e-9)
        g.update(runs=24, passed=reference['passed'], mean_task_latency_seconds=reference['mean_latency_seconds'],
                 max_input_per_run=distribution([r['max_input_tokens'] for r in runs]),
                 context_phases={name: summarize(part) for name, part in (
                     ('root_initial', [r for r in subset if r['root_role'] and r['initial_context']]),
                     ('root_continuation', [r for r in subset if r['root_role'] and not r['initial_context']]),
                     ('expert_initial', [r for r in subset if not r['root_role'] and r['initial_context']]),
                     ('expert_continuation', [r for r in subset if not r['root_role'] and not r['initial_context']])) if part})
        groups[condition] = g
    assert sum(r['cost_micro_cny'] for r in rows) == audit['paid_micro_cny'] == 12140315
    consumed.update({(STUDY / n).relative_to(ROOT).as_posix(): sha(STUDY / n) for n in ('summary.json', 'registration.json')})
    assert all(sha(ROOT / n) == h for n, h in consumed.items())
    document = {'created_at': datetime.now(timezone.utc).isoformat(), 'analysis_type': 'post_hoc_descriptive_profile',
                'all_checks_passed': True, 'new_model_calls': 0, 'new_cost_cny': 0,
                'frozen_inputs_unchanged': True, 'prior_shared_hashes_checked': 172,
                'ledger_before': before, 'ledger_after': after, 'unique_paid_rows_profiled': len(ids),
                'definitions': {'input_tokens': 'Provider prompt_tokens; includes API formatting/tools, not a local token estimate.',
                    'output_tokens': 'Provider completion_tokens; reasoning_tokens are a reported subset, never added again.',
                    'message_characters': 'Python Unicode character length of saved compact JSON messages; excludes separate tool schemas.',
                    'latency': 'Non-streaming complete API call and whole task are separate; no TTFT/thinking-time measurement.',
                    'thresholds': 'Strictly greater than 8192/16384/32768/65536 input tokens. Descriptive bins, not model limits.',
                    'context_phases': 'Initial means exactly system+user messages at root or expert loop start; no cache inference.',
                    'p95': 'Sorted value at ceil(0.95*n), one-based nearest rank.',
                    'denominator': 'Every paid call of all 144 first attempts, including the failed business task.'},
                'overall': summarize(rows), 'groups': groups, 'runs': run_rows,
                'largest_inputs': sorted(rows, key=lambda r: r['input_tokens'], reverse=True)[:10],
                'limitations': ['Repeated API turns from 24 synthetic states are not independent user samples.',
                    'No long-context retention, evidence-position randomization or distractor stress test was run.',
                    'Version 6 combines three interventions; these descriptive differences are not causal ablations.',
                    'No model weights, positional embeddings or serving context limits were changed.',
                    'This profile does not alter frozen study scores or validate all free text.'],
                'source_sha256': consumed, 'profile_source_sha256': sha(Path(__file__).resolve())}
    report = render(document)
    with CSV.open('x', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    document['call_table_sha256'] = sha(CSV)
    with OUT.open('x', encoding='utf-8') as f:
        json.dump(document, f, ensure_ascii=False, indent=2); f.write('\n')
    with REPORT.open('x', encoding='utf-8') as f:
        f.write(report)
    print(json.dumps({'all_checks_passed': True, 'calls': len(rows), 'runs': len(run_rows),
                      'max_input_tokens': document['overall']['input_tokens']['max'],
                      'threshold_counts': document['overall']['calls_above_input_threshold'],
                      'new_cost_cny': 0, 'ledger_cny': before[0]/1000000}, ensure_ascii=False))


def render(d):
    lines = ['# 已保存任务的上下文、费用与时延分析', '',
        '2026-09-09。读取运输提案可靠性实验的全部 144 次首次尝试、745 次模型调用，逐笔核对原始 trace、消息散列与预算账本。'
        '本分析新增 API 调用 0 次，新增模型费用 0 元；冻结分数不变。', '',
        f"已观察的最长单次输入为 **{d['overall']['input_tokens']['max']:,} token**。这是这批任务实际消耗的上下文长度，不是模型能力上限。"
        '没有进行证据位置随机化、长文干扰或记忆保持实验，因此不能据此宣称支持长上下文业务。', '',
        '## 每次模型调用', '',
        '| 条件 | 调用数 | 平均输入 token | 输入中位数 | 输入 P95 | 最大输入 | >16,384 次数 | 平均 API 耗时（秒） |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for condition, g in d['groups'].items():
        v, arm = condition.split('_', 1); t = g['input_tokens']
        lines.append(f"| {v} · {LABELS[arm]} | {g['calls']} | {t['mean']:.1f} | {t['median']:g} | {t['p95_nearest_rank']} | {t['max']} | {g['calls_above_input_threshold']['16384']} | {g['api_latency_seconds']['mean']:.3f} |")
    lines += ['', '输入 token 使用服务返回的 prompt_tokens，含服务计入的工具定义和协议内容；消息字符数只用于完整性核查，没有换算成 token。'
        '输出 token 已包含服务报告的 reasoning_tokens，不能重复相加；reasoning token 数也不是思考时长。'
        '这里的 API 耗时包含整次请求，未观测 TTFT、纯解码或独立思考时间。', '',
        '## 每个任务与版本差异', '',
        '| 条件 | 通过/尝试 | 模型调用/任务 | 总输入 token | 总输出 token | 平均费用（元/尝试） | 平均任务耗时（秒） |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for condition, g in d['groups'].items():
        v, arm = condition.split('_', 1)
        lines.append(f"| {v} · {LABELS[arm]} | {g['passed']}/24 | {g['calls']/24:.3f} | {g['input_tokens']['sum']:,} | {g['output_tokens']['sum']:,} | {g['cost_micro_cny']/24/1000000:.6f} | {g['mean_task_latency_seconds']:.3f} |")
    lines += ['', '| v6 相对 v5 | 模型调用变化 | 总输入变化 | 总费用变化 | 平均任务耗时变化 |', '|---|---:|---:|---:|---:|']
    for arm, label in LABELS.items():
        old, new = d['groups']['v5_'+arm], d['groups']['v6_'+arm]
        changes = [new['calls']/old['calls']-1, new['input_tokens']['sum']/old['input_tokens']['sum']-1,
                   new['cost_micro_cny']/old['cost_micro_cny']-1, new['mean_task_latency_seconds']/old['mean_task_latency_seconds']-1]
        lines.append('| ' + label + ' | ' + ' | '.join(f'{v:+.2%}' for v in changes) + ' |')
    lines += ['', '调用次数减少并不保证总输入或总费用减少：新版把订单对象目录、引用建议和程序对照带入后续上下文，'
        '可能使单次请求更长。以上是同状态下各一次轨迹的事后描述，不能把所有变化归因于上下文长度或某一项提示。'
        '每组分母都是全部 24 次任务，包含旧版协调员未完成的那一次。', '',
        '## 可以支持的结论与仍需验证的能力', '',
        '| 问题 | 当前证据 | 未覆盖范围 |', '|---|---|---|',
        '| 服装变体与业务规则 | 原324次与277变体上的独立216次研究分别保留 | 真实库存、真实品牌授权、自然语言需求录入的独立分布 |',
        '| 运输事件与修订 | 本次144次复核及独立路线、状态和程序解释审计 | 多仓、随机承运时间、真实订舱、所有自由文本事实 |',
        '| 执行策略 | 相同状态、模型和工具下三策略对照 | 按需组未实际委派，尚不能证明路由决策优于单 Agent |',
        '| 长上下文 | 745次已记录请求的实际长度分布 | 不同长度、证据位置、干扰密度下的保持能力与违规率 |',
        '| 外部评估工具 | τ-bench回放适配于早期commerce_lab业务；本批服装使用独立程序审计 | 不能把早期τ-bench回放称为新版服装的外部工具复核 |', '',
        '如果后续扩展长上下文能力，应先固定独立服装案例和必需证据，分别改变长度、证据位置与无关资料，'
        '再比较完成、引用、约束违规、费用和时延。此处只是列出验收缺口，没有新建付费实验或修改预登记。'
        'RoPE/PI/YaRN属于模型位置编码方法；本项目通过外部API调用模型，未实现或训练这些方法。', '',
        '[逐次调用表](../evidence/apparel_context_calls_20260909.csv) · [复核JSON](../evidence/apparel_context_profile_20260909.json) · '
        '[原144次结果](APPAREL_RELIABILITY_RESULTS.md) · [独立审计](../evidence/apparel_reliability_audit_20260909.json) · '
        '[前轮216次研究](APPAREL_EXPANSION_RESULTS.md) · [早期τ适配代码](../evaluation/tau_bridge.py)', '',
        '全项目账本及预留仍为339.751400/480元，选型64.986070/100元不变。真实用户为0；Finance-Agent暂停。', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    main()

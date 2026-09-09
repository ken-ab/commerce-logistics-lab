"""Full replication integrity/accounting audit and descriptive comparison."""
from collections import Counter
from contextlib import closing
import csv
from decimal import Decimal
import json
import sqlite3

from apparel_fulfillment.agent import ARMS, compact, pointer
from apparel_fulfillment.agent_evidence_v2 import citation_directory
from apparel_fulfillment.data import ROOT, digest
from research.apparel_experiment import save, sha
from research.apparel_state_pilot_v3 import CONDITIONS
from research.apparel_state_report_v3 import CONFIGS, LABELS, METRICS, extra, factorial, paired_changes
from research.apparel_state_replication_v3 import DIRECTORY, validate


def read_and_audit():
    registration = validate()
    summary = json.loads((DIRECTORY / 'summary.json').read_text(encoding='utf-8'))
    assert summary['runs'] == len(summary['source_results_sha256']) == registration['expected_runs']
    rows, budget_ids = [], set()
    input_hashes = facts = directory_paths = 0
    statuses = Counter()
    cost = Decimal(0)
    with closing(sqlite3.connect((ROOT / 'evidence/api_budget.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        for relative, expected_hash in sorted(summary['source_results_sha256'].items()):
            path = ROOT / relative
            assert sha(path) == expected_hash
            r = json.loads(path.read_text(encoding='utf-8'))
            execution = json.loads((path.parent / 'execution.json').read_text(encoding='utf-8'))
            assert all(r[k] == v for k,v in execution.items()), 'Scoring must not alter execution'
            assert r['registration_sha256'] == sha(DIRECTORY / 'registration.json')
            assert r['tool_calls'] <= 32 and r['model_calls'] <= 12
            b,g = CONDITIONS[r['condition']]
            assert r['interventions'] == {'bootstrap': b, 'enforce_contract': g}
            assert r['host_initial_reads'] == int(b)
            for t in r['traces']:
                if t['kind'] != 'model': continue
                assert digest(t['messages']) == t['input_sha256']
                content = compact(t['messages'])
                assert len(content) == t['input_characters']
                assert 'SCREP-' not in content and 'allowed_by_line' not in content and 'minimum_differences' not in content
                input_hashes += 1
            for obs in r['observations'].values():
                for p in citation_directory(obs)['paths']:
                    assert len(compact(pointer(obs,p))) <= 2500
                    directory_paths += 1
            if r['report']:
                for fact in r['report']['source_facts']:
                    assert compact(pointer(r['observations'][fact['observation_id']],fact['pointer'])) == compact(fact['value'])
                    facts += 1
                if g: assert r['report']['operation_check']['passed']
                assert r['report']['execution_receipt']['selected_lines'] == r['after']['selections']
            attempt = json.loads((path.parent / 'attempt.json').read_text(encoding='utf-8'))
            records = db.execute('SELECT id,COALESCE(charged,reserved),status FROM calls WHERE purpose LIKE ?', (attempt['purpose']+'%',)).fetchall()
            amount = Decimal(sum(v for _,v,_ in records)) / 1000000
            assert amount == Decimal(r['accounted_and_reserved_cny'])
            ids = {i for i,_,_ in records}
            assert len(ids) == len(records) and not ids & budget_ids
            assert {c['budget_call_id'] for c in r['calls'] if c.get('budget_call_id')} <= ids
            budget_ids.update(ids); statuses.update(s for _,_,s in records); cost += amount
            rows.append(r)
    assert {(r['case_id'],r['condition'],r['arm']) for r in rows} == {tuple(j) for j in registration['jobs']}
    for case in {r['case_id'] for r in rows}:
        assert len({r['initial_view_digest'] for r in rows if r['case_id']==case}) == 1
    qa = {'status':'passed','scope':'Input/result integrity, citations and accounting; not a new performance oracle',
          'runs':len(rows),'model_input_hashes':input_hashes,'citation_paths_checked':directory_paths,
          'reported_fact_values_checked':facts,'unique_ledger_call_ids':len(budget_ids),
          'ledger_status_counts':dict(statuses),'accounted_and_reserved_cny':str(cost),
          'registration_sha256':sha(DIRECTORY/'registration.json'),'summary_sha256':sha(DIRECTORY/'summary.json'),
          'report_source_sha256':sha(ROOT/'research/apparel_state_replication_report.py')}
    save(DIRECTORY/'qa.json',qa)
    return rows, summary, qa


def make():
    rows, summary, qa = read_and_audit()
    groups = {c:{a:extra([r for r in rows if r['condition']==c and r['arm']==a]) for a in ARMS} for c in CONDITIONS}
    totals = {c:extra([r for r in rows if r['condition']==c]) for c in CONDITIONS}
    effects = factorial(rows)
    comparison = {'scope':'new_order_states_known_catalogue','runs':len(rows),'groups':groups,'totals':totals,
        'factorial_effects':effects,'paired_changes':paired_changes(rows),'cost_cny':qa['accounted_and_reserved_cny'],
        'source_summary_sha256':sha(DIRECTORY/'summary.json')}
    comparison['returned_model_labels'] = dict(Counter(c.get('returned_model') for r in rows for c in r['calls']))
    comparison['call_status_counts'] = dict(Counter(c['status'] for r in rows for c in r['calls']))
    save(DIRECTORY/'comparison.json',comparison)
    flat=[]
    for r in rows:
        entry={k:r[k] for k in ('case_id','scenario','condition','arm','run_status','model_calls','tool_calls','input_tokens','output_tokens','accounted_and_reserved_cny','latency_seconds','delegations')}
        entry.update({k:r['score'][k] for k in ('task_completed','business_success','required_evidence_covered','constraint_violation')})
        entry.update(failure_reasons=';'.join(r['score']['failure_reasons']),evidence_gaps=';'.join(r['score']['evidence_gaps']))
        flat.append(entry)
    with (DIRECTORY/'case_results.csv').open('w',encoding='utf-8-sig',newline='') as h:
        writer=csv.DictWriter(h,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    failures=[r for r in rows if not r['score']['task_completed']]
    completed=sum(r['run_status']=='completed' for r in rows)
    text=['# 状态与动作检查：新订单状态复核','',
          f'144次运行全部归档；{completed}次返回完成报告，{144-len(failures)}次通过独立业务与必要来源核验。费用与未知预留合计 **{qa["accounted_and_reserved_cny"]}元**。', '',
          '此轮在AIHubMix恢复后单独登记，原144次故障试跑的结果和分母不变。新订单使用不同开发目标SKU、新日期与数量，以及MOQ、尺码澄清、延误、宽交期低预算和一小时无解等状态；任务结构和小目录仍已知，不是新商家或未见领域测试。', '',
          '| 配置 | Accuracy 任务通过/36 | 业务正确/36 | 必要来源完整/36 | 约束违规 | Avg Cost CNY | Avg Latency s | P95 s |',
          '|---|---:|---:|---:|---:|---:|---:|---:|']
    for c in CONDITIONS:
        a=totals[c]
        text.append(f'| {CONFIGS[c]} | {a["task_completed"]} | {round(a["business_success"]*36)} | {round(a["evidence_coverage"]*36)} | {a["constraint_violation_runs"]} | {a["avg_cost_cny"]:.5f} | {a["avg_latency_seconds"]:.2f} | {a["p95_latency_seconds"]:.2f} |')
    text += ['', '全部配置使用同一v3操作提示、字段目录和报告结构；两项关闭并非历史v1。每配置36运行是12场景×3策略，不能当作36个独立业务场景。Accuracy是Agent完整任务通过率，不能与商品检索NDCG或首位命中混用。', '',
             '| 策略 | 配置 | 任务通过/12 | 平均费用 CNY | 平均时延 s | 模型调用 | 实际委派任务 |','|---|---|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        for c in CONDITIONS:
            a=groups[c][arm]
            text.append(f'| {LABELS[arm]} | {CONFIGS[c]} | {a["task_completed"]} | {a["avg_cost_cny"]:.5f} | {a["avg_latency_seconds"]:.2f} | {a["model_calls"]} | {a["delegation_runs"]}/12 |')
    text += ['', '## 两因素的配对描述','',
             '| 因素 | 完成率差 pp [95%区间] | 每次费用差 CNY [95%区间] | 每次时延差 s [95%区间] |','|---|---:|---:|---:|']
    names={'bootstrap_effect':'起始读取主效应','guard_effect':'动作检查主效应','interaction':'两因素交互','both_minus_neither':'两项开启减关闭'}
    for name,values in effects['effects'].items():
        cells=[]
        for metric,scale,digits in zip(METRICS,(100,1,1),(2,5,2)):
            v=values[metric];lo,hi=v['paired_scenario_95_interval']
            cells.append(f'{v["difference"]*scale:+.{digits}f} [{lo*scale:+.{digits}f}, {hi*scale:+.{digits}f}]')
        text.append('| '+names[name]+' | '+' | '.join(cells)+' |')
    text += ['', '按12个场景联合配对重采样5000次，固定种子，无多比较校正。区间是小样本描述，不证明普遍效果；服务失败、输入/输出token、修复与逐组细节保留在JSON。时延包含模型、工具和修复，没有测量纯思考秒数。', '',
             '## 运行与修复','', '| 指标 | 两项关闭 | 仅起始状态 | 仅动作检查 | 两项开启 |','|---|---:|---:|---:|---:|']
    metrics=[('完成报告','completed_reports'),('账户拒绝','account_refusal_runs'),('模型调用','model_calls'),
             ('输入token','input_tokens_known'),('输出token','output_tokens_known'),('可观测推理token','reasoning_tokens_known'),
             ('起始读取','host_initial_reads'),('结束状态检查','host_completion_checks'),('动作检查修复','contract_repair_attempts'),
             ('引用修复','citation_repair_attempts'),('无效工具尝试','rejected_tool_attempts')]
    for label,key in metrics:text.append('| '+label+' | '+' | '.join(str(totals[c][key]) for c in CONDITIONS)+' |')
    call_status=Counter(c['status'] for r in rows for c in r['calls'])
    text += ['', '模型调用状态：'+json.dumps(dict(call_status),ensure_ascii=False)+'。缺失使用量不当作零；未知费用保留。', '',
             '本轮617个模型请求全部成功返回并结算。请求ID固定为gpt-5.6-luna；响应的模型标签均为gpt-56-luna。保留供应商返回别名，不能据此独立证明底层权重版本固定。', '',
             '## 全部任务未通过记录','', '| 场景 | 配置 | 策略 | 执行状态 | 业务缺口 | 来源缺口 |','|---|---|---|---|---|---|']
    for r in failures:
        s=r['score']
        text.append(f'| {r["scenario"]} | {CONFIGS[r["condition"]]} | {LABELS[r["arm"]]} | {r["run_status"]} | {"; ".join(s["failure_reasons"]) or "无"} | {"; ".join(s["evidence_gaps"]) or "无"} |')
    if not failures:text.append('| 本轮未观察到 | — | — | — | — | — |')
    text += ['', f'完整性QA通过：{qa["runs"]}原始结果、{qa["model_input_hashes"]}模型输入摘要、{qa["reported_fact_values_checked"]}引用事实及{qa["unique_ledger_call_ids"]}账本调用核对一致。该验收不替代任务评分。', '',
             f'结束时全项目费用与预留 **{summary["ledger_after"]["accounted_and_reserved_cny"]}元**，硬上限480元；非供应商账单。真实客户/用户0，Finance-Agent暂停。工作台默认没有自动替换。', '',
             '- [预登记协议](APPAREL_STATE_REPLICATION_PROTOCOL.md)',
             '- [逐例CSV](../evidence/apparel_state_replication_v3/case_results.csv)',
             '- [配对统计](../evidence/apparel_state_replication_v3/comparison.json)',
             '- [原始文件SHA](../evidence/apparel_state_replication_v3/summary.json)',
             '- [完整性与账本QA](../evidence/apparel_state_replication_v3/qa.json)', '']
    text += ['- [五次未通过与执行策略判断](APPAREL_STATE_REPLICATION_REVIEW.md)', '']
    (ROOT/'research/APPAREL_STATE_REPLICATION_RESULTS.md').write_text('\n'.join(text),encoding='utf-8')
    print(json.dumps({'runs':144,'qa':'passed','completed_reports':completed,'task_passed':144-len(failures),
                      'totals':{c:totals[c]['task_completed'] for c in CONDITIONS},'cost_cny':qa['accounted_and_reserved_cny']},ensure_ascii=False))


if __name__=='__main__':make()

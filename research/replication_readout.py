"""Read completed, frozen experiments without changing a trial or selecting on test."""
import argparse
from collections import Counter
import csv
from decimal import Decimal
import json
from pathlib import Path

from audit_replication.assess import read_phase
from audit_replication.method import validate_final
from evaluation.business_metrics import paired
from research.model_config import ROOT


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def build(partition):
    if partition == 'test':
        validate_final()
    result = read_phase(partition)
    directory = Path(result['directory'])
    names = {'identity_multi':'自由报告对照', 'structured_multi':'来源与状态报告'}
    fields = ('business','facts','communication','joint')
    captions = {'business':'业务状态通过','facts':'报告事实获支持','communication':'信息与语言通过','joint':'三项同时通过'}
    comparisons, exports = {}, []
    for arm, item in result['arms'].items():
        raw = json.loads((directory/arm/'results.json').read_text(encoding='utf-8-sig'))
        by_id = {r['case_id']:r for r in raw}
        for row in item['rows']:
            audit = by_id[row['case_id']]['audit']
            exports.append({'arm':arm, **row,
                'fact_verdict':audit.get('facts',{}).get('decision',{}).get('verdict'),
                'facts_transport_failures':len(audit.get('facts',{}).get('transport_failures',[])),
                'communication_transport_failures':len(audit.get('communication',{}).get('transport_failures',[]))})
        item['fact_verdicts'] = dict(Counter(r['audit'].get('facts',{}).get('decision',{}).get('verdict','no_decision') for r in raw))
        item['fact_statuses'] = dict(Counter(r['audit'].get('facts',{}).get('status','missing') for r in raw))
        item['communication_statuses'] = dict(Counter(r['audit'].get('communication',{}).get('status','missing') for r in raw))
        item['by_family'] = {family:{'cases':len(selected), **{key:sum(r[key] for r in selected) for key in fields}}
            for family in sorted({r['family'] for r in item['rows']})
            for selected in [[r for r in item['rows'] if r['family']==family]]}
        item['by_language'] = {language:{'cases':len(selected), **{key:sum(r[key] for r in selected) for key in fields}}
            for language in ('en','zh') for selected in [[r for r in item['rows'] if r['language']==language]]}
    cases = [{'id':r['case_id'],'product_id':r['product_id']} for r in result['arms']['identity_multi']['rows']]
    for key in fields:
        rows = {arm:[{'case_id':r['case_id'],'score':{'passed':r[key]}} for r in item['rows']]
                for arm,item in result['arms'].items()}
        comparison = paired(rows['identity_multi'],rows['structured_multi'],cases)
        comparison['scope'] = f"{comparison['product_groups']} fresh public product groups sharing eight simulated templates; no real users."
        comparison['analysis_role'] = 'Predeclared business comparison' if key=='business' else 'Descriptive post-run report comparison; no test-based selection'
        comparisons[key] = comparison
    result['paired_comparisons'] = comparisons
    traces = [json.loads(p.read_text(encoding='utf-8-sig')) for p in (directory/'transport').glob('*.json')]
    result['transport'] = {'http_attempts':len(traces),
        'statuses':dict(Counter(t.get('status') for t in traces)),
        'reused_connections':sum(bool(t.get('reused_connection')) for t in traces),
        'failure_phases':dict(Counter(t.get('phase') for t in traces if t.get('status')=='failed'))}
    result['original_v2_gate_passed'] = False
    result['analysis_scope'] = 'All scheduled denominators retained. Original failed validation unchanged. Report comparisons are descriptive; final scores do not reselect the method.'
    stem = 'audit_replication_'+('validation' if partition=='validation' else 'final')+'_readout'
    write_json(ROOT/'evidence'/f'{stem}.json',result)
    with (ROOT/'evidence'/f'{stem}.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(exports[0]))
        writer.writeheader(); writer.writerows(exports)
    n=result['scheduled_per_arm']
    base,candidate=(result['arms'][key] for key in ('identity_multi','structured_multi'))
    phase='独立验证' if partition=='validation' else '最终测试'
    cost_change=float(Decimal(candidate['settled_cost_cny'])/Decimal(base['settled_cost_cny'])-1)*100
    latency_change=(candidate['latency_median_seconds']/base['latency_median_seconds']-1)*100
    lines=[f'# 审核连接修复后的{phase}结果', '',
        f'完成两个版本各{n}例，共{n*2}次业务运行和{n*4}项对应报告审核。这里的“完成”包括保留失败记录，不表示每项审核成功。', '',
        '| 指标 | 自由报告对照 | 来源与状态报告 |', '|---|---:|---:|']
    for key in fields:
        lines.append(f"| {captions[key]} | {base['counts'][key]}/{n} ({base['counts'][key]/n:.2%}) | {candidate['counts'][key]}/{n} ({candidate['counts'][key]/n:.2%}) |")
    lines.extend([f"| 业务已结算估算费用（元） | {base['settled_cost_cny']} | {candidate['settled_cost_cny']} |",
        f"| 业务时延中位数（秒） | {base['latency_median_seconds']:.4f} | {candidate['latency_median_seconds']:.4f} |", '',
        f'来源报告组业务生成费用较对照变化{cost_change:+.2f}%，时延中位数变化{latency_change:+.2f}%。仅比较业务生成；审核另有费用与时延。费用为已结算的本地费率估算，未结算预留计入全局账本，不是供应商发票。', '',
        '| 来源报告减对照 | 差值（百分点） | 商品组 bootstrap 95% 区间（百分点） |', '|---|---:|---:|'])
    for key, item in comparisons.items():
        low,high=item['cluster_percentile_95_interval']
        lines.append(f"| {captions[key]} | {item['pass_rate_difference']*100:+.3f} | [{low*100:.3f}, {high*100:.3f}] |")
    lines.extend(['', '业务差值沿用预登记的商品组重采样方法；报告三项区间为描述性补充，未进行多重检验校正，也不用于最终集上继续挑选版本。组内任务共享模板，区间不是对任意真实用户的保证。', '',
        '## 审核失败与关键场景', ''])
    for arm,item in result['arms'].items():
        lines.extend([f"- {names[arm]}：事实判定{json.dumps(item['fact_verdicts'],ensure_ascii=False)}；事实审核状态{json.dumps(item['fact_statuses'],ensure_ascii=False)}；信息审核状态{json.dumps(item['communication_statuses'],ensure_ascii=False)}。",
            f"- {names[arm]}：关键规则标记{len(item['critical_issues'])}项。提案或恶意文本报告缺失事实支持也会被标记，不能直接等同于发生了订单误确认。"])
    lines.extend(['',f"审核网络记录：{result['transport']['http_attempts']}次HTTP尝试，{result['transport']['reused_connections']}次连接复用；状态{json.dumps(result['transport']['statuses'],ensure_ascii=False)}，失败阶段{json.dumps(result['transport']['failure_phases'],ensure_ascii=False)}。公开记录发送阶段和预算预留，不包含密钥。", '',
        '## 各类任务', '', '| 组别 / 任务 | 业务 | 事实 | 信息 | 联合 |', '|---|---:|---:|---:|---:|'])
    for arm,item in result['arms'].items():
        for family,v in item['by_family'].items():
            lines.append('| '+names[arm]+' / '+family+' | '+' | '.join(f"{v[k]}/{v['cases']}" for k in fields)+' |')
    lines.extend(['', '## 方法和结论范围', '',
        '原第二版独立验证仍为失败，不回填超时报告、不改原分。连接修复先用原25个事实与16个信息夹具校准：41/41一致、41次首次请求完成、39次连接复用。短夹具校准不是对长报告可靠性的证明。随后在排除全部既有实验商品的新分区上运行一次独立验证，门槛、两组Agent、业务模型和审核提示保持不变。', '',
        '该独立验证已经通过；最终方法在测试前冻结。最终测试报告所有失败，不根据最终成绩再次调提示或换数据争取通过。模型为GPT-5.6-Luna，事实和信息审核为Qwen3.8-Max；审核器属于模型评估，不等同于人工专家判断。', '',
        '数据是此前未用于本项目业务运行的公开ESCI商品，包含标题带shirt的购物袋等；“服装”范围并不纯净。每组共用八种任务模板，一半中英文；未开展真实用户研究。已知商品ID业务案例不测试自由全库召回。价格、库存、重量、物流及订单为模拟；无真实支付、客户或发货。公开数据也不能排除基础模型预训练接触。', '',
        '来源报告把可写事实限制到读取的商品资料和最终状态，不保证原始商家描述真实；引用截断、缺少偏好历史、多仓拆单、税费和真实退货流程仍是限制。', '',
        f'证据目录：`{directory.relative_to(ROOT).as_posix()}`；逐例CSV：`evidence/{stem}.csv`；复算与来源SHA：`evidence/{stem}.json`。协议：`audit_replication/PROTOCOL.md`。'])
    target=ROOT/'research'/('AUDIT_REPLICATION_VALIDATION_READOUT.md' if partition=='validation' else 'AUDIT_REPLICATION_FINAL_READOUT.md')
    target.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return {'partition':partition,'counts':{a:v['counts'] for a,v in result['arms'].items()},'report':str(target)}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--partition',choices=['validation','test'],required=True)
    print(build(parser.parse_args().partition))

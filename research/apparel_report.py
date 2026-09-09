"""Create an answer-first phase report only from the complete original and supplementary evidence."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json

from apparel_fulfillment.data import ROOT
from delivery_budget import operational_ledger
from research.apparel_experiment import validate

STUDY = ROOT / 'evidence/apparel_strategy_v1'
ARMS = ('single', 'coordinator', 'on_demand')
LABELS = {'single': '单 Agent', 'coordinator': '协调员加专家', 'on_demand': '按需委派'}
FAMILIES = {'product_info': '商品资料查询', 'order_ready': '订单可继续', 'order_clarification': '订单缺少条件',
            'rule_blocked': '地区 / 起订量限制', 'shortage_alternative': '缺货替代', 'shipping_normal': '正常运输',
            'shipping_infeasible': '运输无解', 'shipping_revision': '运输事件修订', 'combined': '替代与运输组合'}
ERRORS = {'decision_status_mismatch': '决定状态不符合实际任务', 'final_proposal_id_missing_or_stale': '最终提案标识缺失或指向旧版',
          'final_route_invalid': '最终路线未通过当前条件校验', 'old_proposal_validity_not_observed': '未取得旧提案的有效性判断',
          'proposal_version_expectation_failed': '没有按要求保留或新增方案版本', 'wrong_or_nonminimal_candidate': '所选候选错误或差异不是最少',
          'required_order_issue_missing': '最终状态缺少应当出现的订单问题', 'read_only_request_mutated': '资料查询时修改了订单',
          'no_completed_report': '没有形成完整可核验报告', 'transport_events_not_observed': '未读取当前运输事件',
          'unrequested_transport_tools': '无运输任务时尝试调用运输工具',
          'specific_order_issue_not_cited': '未引用具体订单问题', 'substitution_differences_not_cited': '替代差异缺少引用',
          'ready_order_status_not_cited': '缺少订单核验状态引用', 'adjustment_options_not_cited': '调整方向依据不完整',
          'current_route_arrival_at_not_cited': '当前版本的到达时间未引用', 'current_route_total_cost_cents_not_cited': '当前版本的费用未引用',
          'requested_product_field_not_cited': '没有引用所问的商品字段', 'invalid_or_missing_final_citations': '没有有效的最终来源引用'}


def read(path): return json.loads(path.read_text(encoding='utf-8'))


def make():
    validate()
    original = read(STUDY / 'test/summary.json')
    audit = read(STUDY / 'test/task_audit_v3.json')
    selection = read(STUDY / 'validation/selection.json')
    if original['status'] != 'complete' or original['runs'] != 324 or audit['status'] != 'complete':
        raise ValueError('Do not generate a final project report from a partial study')
    ledger = operational_ledger()
    with closing(ledger.connect()) as db:
        grouped = db.execute("SELECT purpose,status,COALESCE(charged,reserved),charged FROM calls WHERE purpose LIKE 'commerce_apparel:%'").fetchall()
    accounting = {}
    for purpose, status, amount, charged in grouped:
        phase = purpose.split(':')[1]
        item = accounting.setdefault(phase, {'calls': 0, 'accounted_cny_micros': 0, 'unknown_calls': 0})
        item['calls'] += 1; item['accounted_cny_micros'] += amount; item['unknown_calls'] += charged is None
    selected = selection['selected_arm']
    summaries = audit['arms']
    phase_amount = sum(row[2] for row in grouped) / 1000000
    whole = ledger.summary()
    text = ['# Commerce Logistics Lab：服装订单与跨境履约 Agent 研究', '',
            '服装订单核验、可修订运输提案和三种执行策略对照已完成。本阶段扩展已有系统；Finance-Agent 的实验与修改保持暂停。', '',
            '工作台：[服装订单、运输版本与研究结果](http://127.0.0.1:5176/)。使用方法见 [README_APPAREL.md](../README_APPAREL.md)。', '',
            '## 最终实测', '',
            '108 个独立模拟任务，每个任务分别运行三种策略，共324次。模型、业务工具实现、数据、规则和总调用上限一致，三组初始状态逐例复制并核对。', '',
            '| 策略 | 任务完成：补充口径 | 原v1严格清单 | 平均费用 CNY | 平均时延 s | P95时延 s | 约束违规任务 |',
            '|---|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        s, old = summaries[arm], original['arms'][arm]
        text.append(f'| {LABELS[arm]} | {s["task_completed"]}/108 ({s["task_accuracy"]:.2%}) | {old["task_completed"]}/108 ({old["task_accuracy"]:.2%}) | {s["avg_cost_cny"]:.5f} | {s["avg_latency_seconds"]:.2f} | {s["p95_latency_seconds"]:.2f} | {s["constraint_violation_runs"]} |')
    text += ['', '“任务完成”同时要求正确业务结果和本任务需要的来源依据。它不是检索 NDCG，也不是实际商家成功率。原评分对工具名称和引用粒度过严的部分，已在最终测试前登记补充口径；实现中的一次元数据类型异常另有机械修复记录。原分数、失败、登记时点和全部轨迹保留，见[指标澄清](APPAREL_METRIC_CLARIFICATION.md)。', '',
             f'默认策略按18个验证任务的原规则确定为 **{LABELS.get(selected, "不自动推荐")}**，没有根据最终测试重新挑选。验证原严格清单分别为12/18、12/18、13/18，样本很小。若默认组在测试出现约束违规，工作台证据验收会关闭其自动默认。', '',
             '## 协作的收益与代价', '']
    for arm in ('coordinator', 'on_demand'):
        delta = audit['paired_differences'][arm + '_minus_single']
        quality = delta['task_accuracy']; ci = quality['target_sku_cluster_95_interval']
        phrase = '区间跨过0，不能确认任务完成率有稳定差异。' if ci[0] <= 0 <= ci[1] else '该区间在本模拟集合中未跨过0；仍不能外推所有商家。'
        text += [f'- {LABELS[arm]}相对单 Agent：完成率差 {quality["difference"]*100:+.2f} 个百分点，按目标SKU聚类的95%区间为 [{ci[0]*100:+.2f}, {ci[1]*100:+.2f}]；平均费用差 {delta["avg_cost_cny"]["difference"]:+.5f} 元，平均时延差 {delta["avg_latency_seconds"]["difference"]:+.2f} 秒。{phrase}']
    text += ['', f'按需委派组实际调用专家的任务为 **{summaries["on_demand"]["delegation_runs"]}/108**。本轮观察到的是允许委派的执行策略所作的选择；没有观察到专家被调用，不能据此证明按需协作本身的收益，也不能识别“哪些任务值得委派”的有效边界。每次直接处理的观察依据和理由保存在 routing 轨迹中。', '',
             '本轮两种评分的点估计均为单 Agent 最高，且其平均费用和时延最低。工作台保留验证集预先选定的默认项，是为了不把测试集重新用作调参依据；默认项不代表最终测试中的最优策略。', '',
             '各业务类型每组只有12个案例，以下读数用于定位问题和形成下一轮假设，不据此声称某个类别已经普遍最优。', '',
             '| 业务类型 | 单 Agent：完成/12 · 费用 · 秒 | 协调员加专家 | 按需委派 |', '|---|---:|---:|---:|']
    for name, arms in audit['families'].items():
        text.append('| ' + FAMILIES[name] + ' | ' + ' | '.join(f'{arms[a]["task_completed"]}/12 · ¥{arms[a]["avg_cost_cny"]:.4f} · {arms[a]["avg_latency_seconds"]:.1f}s' for a in ARMS) + ' |')
    text += ['', '## 具体完成了什么', '',
             '⑦ 订单：建立37个公开来源服装变体和3个研究款式分组，区分真实商品文字与模拟单位、库存和商家规则。核验尺码、颜色、品牌、SKU、包装换算、库存、销售地区与MOQ，重复SKU按总数量检查；明确要求发生变化时保留差异并等待具体确认。', '',
             '⑥ 物流：单仓、单走廊下计算班次、转运等待、运输时长、容量、预算和交期。取消/延迟/错过班次可使旧提案失效；修订使用原约束，保留旧版和事件。无解时给出需要用户选择的调整方向。确认前重新核验，并用原子库存更新防止并发超卖和重复扣减。独立路线校验器不调用路线规划器；它验证可行性和算术，不宣称证明全局最优。', '',
             '② 编排：实现单执行者、协调员加商品/物流/服务专家、默认直接处理并按观察决定委派。专家调用计入同一个12次模型调用上限；模型无权批准替代、改规则/库存/运输事件或确认订单。公开记录工具轨迹、委派理由、状态版本、引用修复和失败。', '',
             '实际浏览器验收中，20件黑色M码圆领缺货，改为V领需要具体批准；取消空运后，旧提案不可确认，新方案仍为模拟USD173，送达晚一天，最终生成一个幂等的模拟订单。[浏览器完整记录](../evidence/apparel_browser_acceptance_20260908.json)。', '',
             '最终模型界面验收另创建一笔已明确的V领订单：6次模型调用、4次业务工具、29.4秒、0.041857元，生成独立校验有效的模拟运输提案并保持未确认。下载的完整运行文件与服务保存记录相同；此验收不计入324次测试。[模型界面验收](../evidence/apparel_agent_browser_acceptance_20260908.json)。42项本阶段检查全部通过，[JUnit记录](../evidence/apparel_integration_20260908.xml)。', '',
             '126个模拟案例的免费环境核验全部通过，这是对案例构造和程序的检查，不是126次模型成功。[环境核验](../evidence/apparel_fixture_audit_v2/summary.json)。', '',
             '## 失败与解释限制', '']
    for arm in ARMS:
        s = summaries[arm]
        combined = Counter(s['failure_reasons']) + Counter(s['evidence_gaps'])
        top = '；'.join(f'{ERRORS.get(code, code)} {count}次' for code, count in combined.most_common(6)) or '该口径下没有登记失败'
        text += [f'- {LABELS[arm]}：{top}。模型调用成功率 {s["model_call_success"]:.2%}，报告修复 {s["report_repair_attempts"]}次，被拒工具尝试 {s["rejected_tool_attempts"]}次。']
    text += ['', '错误类别允许重叠，不能相加当作失败任务数。[代表性失败逐例复核](APPAREL_FAILURE_REVIEW.md)区分了执行遗漏、最终来源缺失、状态标签错误和不必要的工具调用；该复核没有改变分数或重跑任何模型任务。', '',
             '任务失败与HTTP失败不同：成功返回模型响应，也可能选错候选、漏掉提案或引用不完整。确定性工具拦住操作与模型本身始终正确也不同。最后展示的字段由程序从工具返回中取值；自由理由未做逐句独立事实审核，因此不报告“全文事实准确率100%”。', '',
             '资料只覆盖37个变体；案例是开发者编写的模拟任务，复用了小目录和若干业务模板。测试目标SKU与验证目标不重合，但目录可共同搜索。尚无真实商家、客户、用户、品牌授权、库存、运价、支付、订舱、发货、关税或多仓拆单数据。自然语言用于处理已通过表单确认的结构化订单，尚未实现自由文本订单的自动录入。', '',
             '每个任务/策略只运行一次，不等同于重复采样的稳定性测量。模型API标识和配置相同，但上游没有因此保证不可变的权重快照。时延包含网络和排队；未单独测量首token或思考秒数。', '',
             '## token与费用', '', '| 策略 | 模型调用 | 已知输入token | 已知输出token | 缺usage调用 | 委派任务/108 |', '|---|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        s = summaries[arm]
        text.append(f'| {LABELS[arm]} | {s["model_calls"]} | {s["input_tokens_known"]:,} | {s["output_tokens_known"]:,} | {s["calls_without_returned_usage"]} | {s["delegation_runs"]}/108 |')
    text += ['', '| 本阶段用途 | 调用数 | 计入费用与预留 CNY | 未确认费用调用 |', '|---|---:|---:|---:|']
    for phase, item in sorted(accounting.items()):
        text.append(f'| {phase} | {item["calls"]} | {item["accounted_cny_micros"]/1000000:.6f} | {item["unknown_calls"]} |')
    text += ['', f'报告生成时，服装阶段合计 **{phase_amount:.6f}元**；全项目历史合计 **{whole["accounted_and_reserved_cny"]}元**，含未确认请求的完整预留。均为共享账本估计，非平台结算账单。全项目480元、原100模型选型子任务100元上限不变。', '',
             '## 可用于项目经历的表述', '',
             '**中文**：在已有公开商品检索与模拟履约系统上，扩展服装变体和订单约束核验、缺货替代确认、跨境运输事件处理与提案版本管理；构建独立路线校验及可追踪的工具执行记录，在108个模拟业务案例上完成单Agent、协调员加专家和按需委派三组共324次固定模型对照，分析任务可靠性、来源完整性、token、费用与时延的取舍。', '',
             '**English**: Extended a source-linked commerce prototype with apparel-variant and order-constraint verification, explicit substitution approval, and versioned cross-border fulfillment proposals under transport disruptions. Built an independent route validator and traceable tool execution, and compared single-agent, coordinator–specialist, and on-demand delegation strategies on 108 simulated business cases (324 fixed-model runs), reporting reliability, evidence coverage, token usage, cost, latency, and failures.', '',
             '## 文件与来源', '',
             '- [v1原始最终报告](../evidence/apparel_strategy_v1/test/readout.md)与[逐例CSV](../evidence/apparel_strategy_v1/test/case_results.csv)。',
             '- [补充口径全部结果、逐例评分和原始文件哈希](../evidence/apparel_strategy_v1/test/task_audit_v3.json)。',
             '- [实验登记](APPAREL_EXPERIMENT_PROTOCOL.md)、[补充口径](APPAREL_METRIC_CLARIFICATION.md)；各case/策略目录保存原始模型/工具轨迹及数据库。',
             '- [独立结果图SVG](../artifacts/apparel_research_20260908/orchestration_comparison.svg)。', '',
             '商品来源为[Amazon ESCI](https://github.com/amazon-science/esci-data)。方法参考[ReAct原论文](https://arxiv.org/abs/2210.03629)的观察与行动循环、[LangChain官方多Agent说明](https://docs.langchain.com/oss/python/langchain/multi-agent)的角色与上下文区分，以及[τ-Bench官方仓库](https://github.com/sierra-research/tau2-bench)对策略、工具、任务和轨迹的划分。本项目的具体案例与评分由本项目定义，不能当成这些外部基准的官方分数。', '',
             '生成时间：' + datetime.now(timezone.utc).isoformat()]
    path = ROOT / 'research/APPAREL_RESULTS.md'
    path.write_text('\n'.join(text) + '\n', encoding='utf-8')
    (STUDY / 'phase_accounting.json').write_text(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'phases': accounting, 'phase_accounted_cny': phase_amount, 'whole_project': whole}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(path), 'phase_accounted_cny': phase_amount, 'whole_project': whole['accounted_and_reserved_cny']}, ensure_ascii=False))


if __name__ == '__main__': make()

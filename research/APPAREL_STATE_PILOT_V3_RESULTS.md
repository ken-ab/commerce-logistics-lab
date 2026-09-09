# 订单状态与动作检查：144次开发试跑

12个开发场景×四种配置×三种策略全部记录。新增调用费用及预留共 **7.084889元**。所有比较共用v3操作提示、逐行选择快照与字段目录；“两项关闭”并非历史v1。此轮不替换已验收工作台默认。

**本轮不能确认算法提升。** 113次返回完成报告，其中111次通过任务评分；28次因AIHubMix账户余额不足返回HTTP 403，另3次进程中断。全部保留在144分母，未重试。两项开启29/36、关闭27/36受到服务故障影响，不能把差值解释为检查机制带来的收益。服务为aihubmix.com上的gpt-5.6-luna，与阿里云余额无关。

| 配置 | 任务完成/36 | 业务正确/36 | 必要来源完整/36 | 约束违规 | 平均费用 CNY | 完整耗时均值 s | P95 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| 两项关闭 | 27 | 28 | 27 | 0 | 0.04271 | 16.12 | 42.34 |
| 仅起始状态 | 28 | 29 | 28 | 0 | 0.05006 | 16.07 | 32.94 |
| 仅动作检查 | 27 | 27 | 27 | 0 | 0.05459 | 18.83 | 40.15 |
| 两项开启 | 29 | 29 | 29 | 0 | 0.04945 | 17.99 | 42.65 |

**执行中断：3次。** 原进程退出时100次已完成、3次正在执行、41次未开始。3次按未完成记录，保留持久轨迹及全额未知预留；仅续跑未开始的41次。终止原因未证实。此处耗时均值/P95仅用完整计时，各配置有效数依次为36/35/34/36。原summary的全量时延包含3条下界，不能当完整时延。详见[恢复与计时处理](APPAREL_STATE_V3_INTERRUPTION.md)。

**服务故障明细**：28次余额拒绝集中在2026-09-08 18:16:39–18:16:51（上海时间）。旧批量驱动没有在首次账户错误后停止排队任务，后续独立任务也遭到拒绝；这是运行控制缺陷，不是28次模型业务判断错误。费用保留28次不确定预留及3次中断预留。时延统计包含快速403，不能据此宣称响应更快。

| 配置 | 完成报告/36 | 账户拒绝/36 | 进程中断/36 | 完成报告中的任务通过（仅条件描述） |
|---|---:|---:|---:|---:|
| 两项关闭 | 28 | 8 | 0 | 27/28 |
| 仅起始状态 | 29 | 6 | 1 | 28/29 |
| 仅动作检查 | 27 | 7 | 2 | 27/27 |
| 两项开启 | 29 | 7 | 0 | 29/29 |

完成报告中的比例排除了基础设施失败，只能辅助诊断，不能替换主分母；仅1个场景全部12配置均返回报告，不能凭完整场景重建可靠对照。按需策略实际委派0次，本轮没有验证按需协作收益。

每个配置含12个业务场景的三种策略，不能当作36个独立业务场景。任务完成同时要求正确业务状态及必要来源；模型返回输出和HTTP成功另计。所有失败都在分母。

| 策略 | 配置 | 完成/12 | 原严格/补充完成 | 平均费用 CNY | 平均时延 s | 模型调用总数 | 委派任务数 |
|---|---|---:|---:|---:|---:|---:|---:|
| 单Agent | 两项关闭 | 8 | 5/8 | 0.04014 | 10.67 | 31 | 0/12 |
| 单Agent | 仅起始状态 | 8 | 6/8 | 0.04033 | 11.25 | 30 | 0/12 |
| 单Agent | 仅动作检查 | 8 | 6/8 | 0.04812 | 12.96 | 38 | 0/12 |
| 单Agent | 两项开启 | 8 | 6/8 | 0.04491 | 9.21 | 25 | 0/12 |
| 协调员加专家 | 两项关闭 | 8 | 7/8 | 0.05453 | 22.06 | 53 | 9/12 |
| 协调员加专家 | 仅起始状态 | 9 | 7/9 | 0.06510 | 22.14 | 52 | 9/12 |
| 协调员加专家 | 仅动作检查 | 9 | 7/9 | 0.06769 | 25.32 | 66 | 10/12 |
| 协调员加专家 | 两项开启 | 11 | 8/11 | 0.06038 | 29.39 | 65 | 12/12 |
| 按需委派 | 两项关闭 | 11 | 8/11 | 0.03345 | 15.62 | 46 | 0/12 |
| 按需委派 | 仅起始状态 | 11 | 8/11 | 0.04475 | 15.34 | 46 | 0/12 |
| 按需委派 | 仅动作检查 | 10 | 8/10 | 0.04797 | 18.26 | 49 | 0/12 |
| 按需委派 | 两项开启 | 10 | 8/10 | 0.04304 | 15.38 | 39 | 0/12 |

主开发指标在冻结语义口径上增加独立的逐行期望选择、实际选择和报告选择核对。保留前两份分数，不把运行时动作检查本身当作答案或评分器。新字段带来的标准变化已在调用前登记，因此不能直接拿本轮完成率与历史324次或54次相比。

## 两个因素的配对分析

下表保留预设描述分析；区间不消除集中发生的账户故障，不能用来作算法因果判断或晋级依据。

| 因素 | 完成率差 pp（95%区间） | 每次费用差 CNY（95%区间） | 每次时延差 s（95%区间） |
|---|---:|---:|---:|
| 起始读取主效应 | +4.17 [-9.72, +18.06] | +0.00110 [-0.00636, +0.00863] | -0.10 [-5.59, +4.69] |
| 动作检查主效应 | +1.39 [-13.89, +18.06] | +0.00564 [-0.00515, +0.01658] | +2.35 [+0.24, +4.63] |
| 两因素交互 | +2.78 [-22.22, +25.00] | -0.01250 [-0.02833, +0.00499] | +3.54 [-2.24, +8.98] |
| 两项开启−两项关闭 | +5.56 [-16.67, +27.78] | +0.00674 [-0.00525, +0.02127] | +2.25 [-3.24, +7.90] |

主效应对另一个开关和三种策略取平均；交互为差中之差。按完整场景联合重采样5000次，任务/费用保留12个场景，配对时延仅用全部配置计时完整的9个场景。区间仅作探索性描述，未作多比较校正。API运行顺序预先打乱、最多3并发，时延含工具、模型和修复；没有测到纯思考秒数。

## 运行代价与检查

| 指标（总数，除特别注明） | 两项关闭 | 仅起始状态 | 仅动作检查 | 两项开启 |
|---|---:|---:|---:|---:|
| 输入token | 504968 | 575227 | 626189 | 567913 |
| 输出token | 21280 | 22194 | 25174 | 23303 |
| 可观测推理token | 3036 | 3413 | 3352 | 3522 |
| 模型调用 | 130 | 128 | 153 | 129 |
| 起始host读取 | 0 | 36 | 0 | 36 |
| 结束状态检查 | 28 | 29 | 32 | 30 |
| 发现未满足操作要求的结束尝试 | 1 | 1 | 5 | 1 |
| 动作检查触发的修复 | 0 | 0 | 5 | 1 |
| 字段引用触发的修复 | 1 | 0 | 1 | 1 |
| 所有被拒绝报告尝试 | 1 | 0 | 6 | 2 |
| 最终报告通过完整操作检查 | 27 | 28 | 27 | 29 |
| 无效工具尝试 | 0 | 3 | 2 | 0 |

检查关闭组也计算并留存诊断，但错误不返回模型、不会自动修改状态。一次拒绝可能同时含引用和动作问题，分类计数不能简单相加。推理token仅采用服务返回字段，缺失不等于0；详情见机器读数。执行回执来自程序，独立于模型引用，未补入模型证据得分。

执行中断分布不均，另列无进程中断场景敏感性分析（仍含HTTP 403，不取代全144分母）：

- 两项关闭：22/27完成；仅含所有配置均无执行中断的场景。
- 仅起始状态：21/27完成；仅含所有配置均无执行中断的场景。
- 仅动作检查：20/27完成；仅含所有配置均无执行中断的场景。
- 两项开启：24/27完成；仅含所有配置均无执行中断的场景。

## 全部未完成运行

基础设施失败后的评分缺口是未完成输出的后果，不逐项归咎于模型能力。

| 场景 | 策略 | 配置 | 失败来源 | 业务评分缺口 | 证据缺口 |
|---|---|---|---|---|---|
| approved_read_only | 单Agent | 仅起始状态 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| approved_read_only | 单Agent | 仅动作检查 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| multiple_lines | 单Agent | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual; scenario_line_assignment_wrong | invalid_or_missing_final_citations; ready_order_status_not_cited |
| multiple_lines | 协调员加专家 | 仅动作检查 | 进程中断 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| multiple_lines | 单Agent | 两项关闭 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual; scenario_line_assignment_wrong; selected_variant_source_not_read | invalid_or_missing_final_citations; ready_order_status_not_cited |
| order_clarification | 协调员加专家 | 仅动作检查 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited |
| order_clarification | 按需委派 | 仅动作检查 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited |
| order_clarification | 协调员加专家 | 两项关闭 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited |
| order_ready | 协调员加专家 | 两项开启 | 账户403 | decision_status_mismatch; final_proposal_id_missing_or_stale; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| order_ready | 按需委派 | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; scenario_line_assignment_wrong; wrong_or_nonminimal_candidate | invalid_or_missing_final_citations; ready_order_status_not_cited |
| order_ready | 单Agent | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; scenario_line_assignment_wrong; wrong_or_nonminimal_candidate | invalid_or_missing_final_citations; ready_order_status_not_cited |
| order_ready | 单Agent | 仅动作检查 | 进程中断 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; scenario_line_assignment_wrong; wrong_or_nonminimal_candidate | invalid_or_missing_final_citations; ready_order_status_not_cited |
| order_ready | 协调员加专家 | 两项关闭 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; scenario_line_assignment_wrong; wrong_or_nonminimal_candidate | invalid_or_missing_final_citations; ready_order_status_not_cited |
| product_info | 协调员加专家 | 仅起始状态 | 账户403 | decision_status_mismatch; no_completed_report | invalid_or_missing_final_citations; requested_product_field_not_cited |
| product_info | 单Agent | 仅动作检查 | 账户403 | decision_status_mismatch; no_completed_report; selected_variant_source_not_read | invalid_or_missing_final_citations; requested_product_field_not_cited |
| product_info | 单Agent | 两项关闭 | 账户403 | decision_status_mismatch; no_completed_report; selected_variant_source_not_read | invalid_or_missing_final_citations; requested_product_field_not_cited |
| rule_blocked | 单Agent | 仅动作检查 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited |
| rule_blocked | 单Agent | 两项关闭 | 账户403 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited |
| shipping_infeasible | 协调员加专家 | 仅起始状态 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_infeasible | 单Agent | 仅起始状态 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_infeasible | 按需委派 | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_infeasible | 单Agent | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_infeasible | 按需委派 | 仅动作检查 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_normal | 单Agent | 两项开启 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_normal | 协调员加专家 | 仅动作检查 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual; selected_variant_source_not_read | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_normal | 单Agent | 两项关闭 | 账户403 | decision_status_mismatch; missing_proposal; no_completed_report; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual; selected_variant_source_not_read | invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_revision | 按需委派 | 仅起始状态 | 账户403 | decision_status_mismatch; final_proposal_id_missing_or_stale; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | current_route_arrival_at_not_cited; current_route_total_cost_cents_not_cited; invalid_or_missing_final_citations; ready_order_status_not_cited |
| shipping_revision | 协调员加专家 | 两项关闭 | 账户403 | decision_status_mismatch; final_proposal_id_missing_or_stale; final_route_invalid; no_completed_report; old_proposal_validity_not_observed; proposal_version_expectation_failed; reported_line_assignment_not_actual; reported_sku_set_not_actual; selected_variant_source_not_read; transport_events_not_observed | current_route_arrival_at_not_cited; current_route_total_cost_cents_not_cited; invalid_or_missing_final_citations; ready_order_status_not_cited |
| shortage_alternative | 协调员加专家 | 仅起始状态 | 进程中断 | decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual | invalid_or_missing_final_citations; specific_order_issue_not_cited; substitution_differences_not_cited |
| shortage_alternative | 单Agent | 仅起始状态 | 完成报告缺口 | 无 | specific_order_issue_not_cited |
| shortage_alternative | 协调员加专家 | 两项关闭 | 完成报告缺口 | 无 | specific_order_issue_not_cited |
| shortage_alternative | 按需委派 | 两项关闭 | 账户403 | alternatives_not_observed; decision_status_mismatch; no_completed_report; reported_line_assignment_not_actual; reported_sku_set_not_actual; required_order_issue_missing; scenario_line_assignment_wrong; wrong_or_nonminimal_candidate | invalid_or_missing_final_citations; specific_order_issue_not_cited; substitution_differences_not_cited |
| valid_keep | 单Agent | 仅起始状态 | 账户403 | decision_status_mismatch; final_proposal_id_missing_or_stale; no_completed_report; old_proposal_validity_not_observed; reported_line_assignment_not_actual; reported_sku_set_not_actual; transport_events_not_observed | current_route_arrival_at_not_cited; current_route_total_cost_cents_not_cited; invalid_or_missing_final_citations; ready_order_status_not_cited |

## 适用范围和证据

28项免费定向测试及12个免费环境预检查通过后才登记付费试跑。开发期的业务场景、数据、提示、评分与运行代码在144次调用期间冻结；结果不重试、不挑选。已批准替代、无关事件、多行错配均在这次场景中。

自由理由没有逐句事实判定；动作检查不能证明候选全局最优，研究侧另做场景期望核验。商品来源公开可追溯，库存、规则、运输和业务任务明确模拟。真实客户/用户仍为0，未开展商家上线实验。需另有未用案例和界面验收才能决定晋级。

完成时全项目本地费用核算与预留 **307.985707元**，上限480元。不是供应商账单；100模型选型未重跑，Finance-Agent保持暂停。

- [试跑协议](APPAREL_STATE_PILOT_V3_PROTOCOL.md)
- [144行逐例CSV](../evidence/apparel_state_pilot_v3/case_results.csv)
- [四条件统计与配对区间](../evidence/apparel_state_pilot_v3/comparison.json)
- [登记的144条原始文件SHA](../evidence/apparel_state_pilot_v3/summary.json)
- [设计决策](../docs/adr/0003-apparel-action-contract.md)

- [失败与修复轨迹复核](APPAREL_STATE_PILOT_V3_FAILURE_REVIEW.md)

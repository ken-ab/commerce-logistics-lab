# 引用字段目录：54次开发试跑结果

新目录版在这9个开发案例、三种策略共27次执行中完成 **21/27**；同期原版完成 **21/27**。两个版本合计54次，费用与预留合计 **1.850792元**。以下结论仅限本次开发试跑，不替代原324次正式研究。

## 固定条件与改动

唯一干预是工具观察增加可复制的字段路径目录。原始业务事实、工具权限、模型、提示、策略及调用上限保持不变。目录不读取期望标签；同一案例6个运行复制相同初始库，运行顺序预先随机登记。9个案例仍使用已知的小型商品目录，仅新增模拟任务组合、数量与日期；不声称新的真实商家或商品分布。

54次全部保留，未按中间分数换提示或重试。免费环境检查9例、已有观察1254条/目录路径9757条均通过；这些免费检查不计为模型成功。预检查中一次无运输状态断言错误在付费前修复，旧现场仍保留。

| 策略 | 版本 | 完成/9：补充口径 | 原严格口径 | 平均费用 CNY | 平均时延 s | 输入token总量 | 引用修复 |
|---|---|---:|---:|---:|---:|---:|---:|
| 单 Agent | 原版 | 6 | 5 | 0.02603 | 19.69 | 111,935 | 4 |
| 单 Agent | 字段目录 | 7 | 6 | 0.02268 | 16.03 | 99,867 | 2 |
| 协调员加专家 | 原版 | 8 | 7 | 0.04975 | 32.00 | 210,486 | 6 |
| 协调员加专家 | 字段目录 | 6 | 5 | 0.03982 | 30.56 | 168,382 | 0 |
| 按需委派 | 原版 | 7 | 5 | 0.03695 | 22.99 | 166,200 | 5 |
| 按需委派 | 字段目录 | 8 | 7 | 0.03041 | 17.46 | 139,227 | 2 |

主读数采用之前已公开的补充任务口径，同时保留原严格清单；二者定义与先前报告一致，此次没有修改评分。完成要求业务结果和必要来源同时合格；HTTP请求成功不能代替任务完成。

| 汇总指标 | 原版27次 | 目录版27次 |
|---|---:|---:|
| 业务结果正确 | 26/27 | 25/27 |
| 实际输入token | 488,621 | 407,476 |
| 实际输出token | 24,240 | 19,191 |
| 模型调用 | 133 | 122 |
| 最终约束违规任务 | 0 | 0 |
| 平均费用 CNY | 0.03758 | 0.03097 |
| 平均时延 s | 24.89 | 21.35 |
| P95时延 s | 44.33 | 42.84 |
| 引用修复次数 | 15 | 4 |
| 无效工具尝试 | 7 | 6 |
| 含库存/规则层级错误的修复 | 13 | 0 |

## 配对差异与边界

- 单 Agent：目录版由失败变成功 1例，由成功变失败 0例，完成状态相同 8例。
- 协调员加专家：目录版由失败变成功 0例，由成功变失败 2例，完成状态相同 7例。
- 按需委派：目录版由失败变成功 1例，由成功变失败 0例，完成状态相同 8例。

每种策略只有9例，且一类任务只有1例，不能判断各业务类别的稳定优劣；同一案例的三种策略也不是三个独立业务样本。费用和时延来自实际API运行，包含目录增量及不同执行轨迹的共同作用；没有测量服务端纯思考时间，不能把端到端时延当思考时长。

目录让特定引用错误更少，但业务结果并未随之改善。人工复核看到：把草稿ID当成提案ID后没有读取订单恢复；文字称已选入替代，实际未调用选择工具；已有订单状态没有进入最终引用。见[三组退步的逐例复核](APPAREL_EVIDENCE_PILOT_FAILURE_REVIEW.md)。这些是后续假设，不把它们全部归因为目录这一单一因素。

当前工作台保留已验收的v1和原验证默认。这轮仅为开发证据，不能直接替换默认或宣称已找到最终最优编排。若继续正式比较，应在此次开发后冻结新的未用任务、代码及评分。

## 全部未完成任务

| 案例 | 策略 | 版本 | 业务错误 | 证据缺口 |
|---|---|---|---|---|
| 替代与运输组合 | 协调员加专家 | directory | 无 | ready_order_status_not_cited |
| 替代与运输组合 | 按需委派 | directory | 无 | ready_order_status_not_cited |
| 替代与运输组合 | 单 Agent | directory | decision_status_mismatch; final_proposal_id_missing_or_stale; final_route_invalid; old_proposal_validity_not_observed; proposal_version_expectation_failed | current_route_arrival_at_not_cited; current_route_total_cost_cents_not_cited; ready_order_status_not_cited |
| 替代与运输组合 | 按需委派 | original | 无 | ready_order_status_not_cited |
| 替代与运输组合 | 单 Agent | original | 无 | ready_order_status_not_cited |
| 运输无解 | 协调员加专家 | directory | 无 | ready_order_status_not_cited |
| 运输无解 | 协调员加专家 | original | 无 | ready_order_status_not_cited |
| 运输无解 | 单 Agent | original | decision_status_mismatch | 无 |
| 缺货替代 | 协调员加专家 | directory | required_order_issue_missing; wrong_or_nonminimal_candidate | specific_order_issue_not_cited |
| 缺货替代 | 单 Agent | directory | 无 | specific_order_issue_not_cited |
| 缺货替代 | 按需委派 | original | 无 | specific_order_issue_not_cited |
| 缺货替代 | 单 Agent | original | 无 | specific_order_issue_not_cited |

上述失败均留在分母，自动字段检查未覆盖自由理由的逐句真实性，不能据此称全文事实准确率100%。

完成时全项目账本费用与预留为 **300.900818元**，额度480元；这是本地调用核算，不是平台账单。Finance-Agent未实验或修改，100模型选型未重跑。

- [预先登记协议](APPAREL_EVIDENCE_PILOT_PROTOCOL.md)
- [54行逐例CSV](../evidence/apparel_evidence_pilot_v2/case_results.csv)
- [完整统计与54条源文件摘要](../evidence/apparel_evidence_pilot_v2/summary.json)
- [免费上下文诊断](APPAREL_CONTEXT_DIAGNOSTIC_20260908.md)

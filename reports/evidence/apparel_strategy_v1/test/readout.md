# 服装订单与跨境履约 · 最终测试结果

108 个模拟任务 × 3 种策略，共 324 次登记运行。所有失败保留分母，初始状态逐例核对一致。

| 策略 | 任务完成 | 业务成功 | 证据完整 | 约束违规 | 平均费用 CNY | 平均时延 s | P95 s | 平均模型调用 | 委派任务 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 单 Agent | 82/108 (75.93%) | 89.81% | 78.70% | 0 | 0.03014 | 19.81 | 31.65 | 4.21 | 0 |
| 协调员加专家 | 70/108 (64.81%) | 83.33% | 67.59% | 0 | 0.05668 | 35.71 | 61.99 | 7.05 | 108 |
| 按需委派 | 74/108 (68.52%) | 79.63% | 76.85% | 0 | 0.03621 | 23.01 | 34.47 | 4.96 | 0 |

任务完成同时要求业务正确和指定来源完整。最终展示字段由程序从工具返回中取值；此处不把自由理由中的每句话都视为已校验事实。

## 分业务类型

| 类型 | 单 Agent | 协调员加专家 | 按需委派 |
|---|---:|---:|---:|
| product_info | 12/12; ¥0.0074; 7.0s | 12/12; ¥0.0153; 13.3s | 10/12; ¥0.0141; 13.3s |
| order_ready | 12/12; ¥0.0289; 20.5s | 12/12; ¥0.0363; 27.8s | 12/12; ¥0.0394; 24.9s |
| order_clarification | 12/12; ¥0.0150; 15.2s | 11/12; ¥0.0312; 33.6s | 12/12; ¥0.0181; 16.7s |
| rule_blocked | 12/12; ¥0.0192; 16.6s | 10/12; ¥0.0504; 37.1s | 11/12; ¥0.0231; 18.5s |
| shortage_alternative | 11/12; ¥0.0538; 26.6s | 8/12; ¥0.0651; 37.4s | 8/12; ¥0.0550; 27.3s |
| shipping_normal | 12/12; ¥0.0341; 21.2s | 7/12; ¥0.0760; 47.5s | 12/12; ¥0.0396; 25.0s |
| shipping_infeasible | 4/12; ¥0.0317; 23.8s | 5/12; ¥0.0573; 41.2s | 3/12; ¥0.0376; 26.8s |
| shipping_revision | 1/12; ¥0.0329; 22.7s | 0/12; ¥0.0658; 38.1s | 0/12; ¥0.0397; 25.2s |
| combined | 6/12; ¥0.0483; 24.7s | 5/12; ¥0.1127; 45.3s | 6/12; ¥0.0593; 29.4s |

## 与单 Agent 配对的差异

正的完成率差值有利于比较组；正的费用或时延差值表示代价更高。下列区间按目标 SKU 聚类重采样，仅反映本模拟集合。

| 比较组减单 Agent | 完成率差及 95% 区间 | 平均费用差 CNY | 平均时延差 s |
|---|---:|---:|---:|
| 协调员加专家 | -11.11 pp [-17.55, -4.63] | +0.02654 | +15.90 |
| 按需委派 | -7.41 pp [-13.76, -1.80] | +0.00607 | +3.19 |

## 失败与调用记录

### 单 Agent

调用成功率 100.00%；已知输入/输出 token 1,575,899/76,409；未返回 usage 的调用 0。引用修正 72 次；被拒工具尝试 14 次。

业务失败：{"selected_variant_source_not_read": 9, "decision_status_mismatch": 1, "required_order_issue_missing": 1, "wrong_or_nonminimal_candidate": 1}

来源缺项：{"adjustment_options_not_cited": 6, "ready_order_status_not_cited": 16, "specific_order_issue_not_cited": 1}

### 协调员加专家

调用成功率 100.00%；已知输入/输出 token 2,866,638/159,895；未返回 usage 的调用 0。引用修正 121 次；被拒工具尝试 24 次。

业务失败：{"unrequested_transport_tools": 2, "required_order_issue_missing": 5, "wrong_or_nonminimal_candidate": 5, "selected_variant_source_not_read": 9, "decision_status_mismatch": 1, "final_proposal_id_missing_or_stale": 1, "no_completed_report": 1, "alternatives_not_observed": 1}

来源缺项：{"adjustment_options_not_cited": 6, "current_route_arrival_at_not_cited": 3, "ready_order_status_not_cited": 22, "substitution_differences_not_cited": 1, "specific_order_issue_not_cited": 6, "current_route_total_cost_cents_not_cited": 1, "invalid_or_missing_final_citations": 1}

### 按需委派

调用成功率 100.00%；已知输入/输出 token 1,963,018/80,121；未返回 usage 的调用 0。引用修正 63 次；被拒工具尝试 23 次。

业务失败：{"decision_status_mismatch": 4, "unrequested_transport_tools": 2, "old_proposal_validity_not_observed": 2, "required_order_issue_missing": 2, "wrong_or_nonminimal_candidate": 2, "selected_variant_source_not_read": 13}

来源缺项：{"adjustment_options_not_cited": 6, "specific_order_issue_not_cited": 3, "ready_order_status_not_cited": 14, "requested_product_field_not_cited": 2}

## 可复核文件与解释范围

[逐例结果 CSV](case_results.csv)；同目录每个 case ID/策略包含原始 `result.json`、初始数据库副本和模型/工具轨迹。`summary.json` 另含逐组费用、时延及全部配对区间。

商品文字来自 Amazon ESCI；款式分组及商家/运输条件为模拟。测试目标 SKU 与验证目标不重合，但目录可共同搜索。案例复用模板且共享目录，不能外推实际商家成功率。

模型 ID/参数固定，不代表上游服务保证不可变权重快照。时延包含网络与服务排队；未测首 token 或真实思考秒数。费用为保守账本估计和预留，非平台发票。

[登记方案](../../../../research/APPAREL_EXPERIMENT_PROTOCOL.md) 说明指标、数据分区、冻结与选择规则。

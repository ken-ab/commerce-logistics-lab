# 服装 Agent 上下文诊断：重复信息与最终引用遗漏

本报告重新读取已冻结的324条测试轨迹，未调用模型、未改业务代码、未重算完成率，新增API费用为0元。它用于提出下一轮假设，不能作为独立测试或改进结果。

诊断改变了优化优先级：删除专家展示文本只减少协调员消息字符的0.94%，不能解释其主要开销。256次报告修复中，175次包含把库存或品牌规则误放在`variant`内部的引用。先验证可引用字段目录与明确的专家子任务边界，比优先引入有损压缩更有依据；这仍是待实验检验的判断。

## 重复读取

同一次运行内，比较同工具、相同参数的前后两次成功只读调用；完整返回JSON值相同才计数。参数不同、库存或版本改变不计为相同。重复返回并不证明再次检查没有必要，因此这里不称其为“浪费调用”。

| 策略 | 成功只读调用 | 返回未变的重复读取 | 涉及任务 /108 | 跨执行角色的重复 |
|---|---:|---:|---:|---:|
| 单 Agent | 278 | 0 | 0 | 0 |
| 协调员加专家 | 351 | 25 | 14 | 23 |
| 按需委派 | 275 | 1 | 1 | 0 |

## 交接内容的重复表示

每次模型输入都按原记录计量；同一条历史消息再次发送会再次计入。字符是JSON序列化后的Unicode字符数，**不是token**，且消息字符不含工具定义或服务端开销。token列来自真实调用usage，不能按字符比例折算费用节省。

| 策略 | 实际输入token | 消息字符总数 | 仅移除专家展示文本的字符差 | 占消息字符 | 可见专家交接 / 无报告 |
|---|---:|---:|---:|---:|---:|
| 单 Agent | 1,575,899 | 4,792,677 | 0 | 0.00% | 0 / 0 |
| 协调员加专家 | 2,866,638 | 8,965,193 | 84,667 | 0.94% | 152 / 64 |
| 按需委派 | 1,963,018 | 5,768,373 | 0 | 0.00% | 0 / 0 |

上述字符差是离线删除专家交接中的`expert_report.answer`字段所得，其余`decision`、`source_facts`、`grounding`和原始观察保留。展示文本由已有字段展开，值得验证能否只在界面生成。该计算没有重新运行模型，不证明删掉文本后模型的行为、费用或准确率保持不变。

“可见交接”只统计确实进入后续模型输入的工具回复，按`tool_call_id`去重；没有形成报告的交接仍可能传回原始观察，不等于其所有工作都丢失。

## 证据已经可见，但最终没有引用

只检查原补充评分中的三类引用缺口，并在最后一次根执行者输入中寻找对应原始观察。运输字段必须匹配最终所报提案ID和当前字段值；订单状态必须匹配最终核验状态。这里计量信息可得性，不重新判分，也不作因果解释。

| 策略 | 订单状态缺口：已可见 / 全部 | 到达时间缺口：已可见 / 全部 | 运输费用缺口：已可见 / 全部 |
|---|---:|---:|---:|
| 单 Agent | 6 / 6 | 0 / 0 | 0 / 0 |
| 协调员加专家 | 9 / 10 | 2 / 3 | 0 / 1 |
| 按需委派 | 3 / 3 | 0 / 0 | 0 / 0 |

分子要求已形成最终报告；没有完成报告的运行另保留在逐例JSON中。0个引用缺口的类别不计算比例。多种缺口可出现在同一任务，不能把列相加当作失败任务数。

## 引用修复与待人工核对项

| 策略 | 引用修复尝试 | 含库存/品牌规则层级错误 | 库存引用SKU与最终候选不同的任务 |
|---|---:|---:|---:|
| 单 Agent | 72 | 60 | 11 |
| 协调员加专家 | 121 | 71 | 16 |
| 按需委派 | 63 | 44 | 13 |

库存SKU不一致仅是复核标记：引用原商品库存来解释替代完全可能合理，不能自动判错。JSON包含每次观察ID、指针、来源SKU和最终候选，须结合实际陈述阅读。引用修复按被拒的`finish`尝试计次；一次尝试可含多个错误字段。

- 单 Agent最常见无效字段：`/result/variant/stock/available_catalog_units` 51次；`/result/variant/brand_rule/allowed_sales_regions` 41次；`/result/variant/brand_rule/wholesale_minimum_pieces_per_sku` 34次；`/result/route/total_cost_cents` 5次；`/result/route/arrival_at` 5次。
- 协调员加专家最常见无效字段：`/result/variant/stock/available_catalog_units` 61次；`/result/variant/brand_rule/allowed_sales_regions` 59次；`/result/variant/brand_rule/wholesale_minimum_pieces_per_sku` 43次；`/result/route/total_cost_cents` 22次；`/result/route/arrival_at` 20次。
- 按需委派最常见无效字段：`/result/variant/stock/available_catalog_units` 41次；`/result/variant/brand_rule/allowed_sales_regions` 30次；`/result/variant/brand_rule/wholesale_minimum_pieces_per_sku` 21次；`/result/route/total_cost_cents` 6次；`/result/route/arrival_at` 6次。

## 下一轮怎样验证

1. 先提供由实际观察生成的可引用字段目录，单独检验能否减少引用层级错误。目录不能生成新事实或替模型执行遗漏的业务动作。另行检验专家只完成受委派子任务、及时返回的边界设计；不要混合改动后声称已识别单一原因。
2. 对最终报告增加与订单、SKU和提案版本绑定的字段检查；把“指针存在”和“引用回答了当前问题”分开。完整交接与只保留结构化交接可作次要消融，用实际token、费用、时延和完成率判断，而非以字符比例代替。
3. 使用旧测试仅作开发诊断；新的正式对照要另行冻结未用案例和评分，保留原v1结果。暂不引入有损提示压缩或新训练模型。

方法依据：LangChain官方[Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)强调输入上下文与返回结果都需设计；[LLMLingua-2](https://arxiv.org/html/2403.12968v2)与[RECOMP](https://arxiv.org/html/2310.04408v1)研究压缩后的任务表现。这些论文并未在本项目复现，不能把论文加速结果写成项目成果。

复现：`python -X utf8 -m research.apparel_context_diagnostic`。脚本逐个核对324个原始结果和方法文件SHA-256，再核对每次模型消息摘要与字符数。

- [逐例JSON与原始文件摘要](../evidence/apparel_context_diagnostic_20260908.json)
- [324行CSV](../evidence/apparel_context_diagnostic_20260908_cases.csv)
- [原始阶段结果](APPAREL_RESULTS.md)
- [七个代表性失败的人工复核](APPAREL_FAILURE_REVIEW.md)

# v3开发试跑：失败与修复复核

144次登记运行全部保留：111次通过、2次完成报告但缺必要依据、28次账户403、3次进程中断。这里是Codex对冻结轨迹的事后分析，不是新的人工专家评分，不改变任何原分。

## 基础设施失败

28次请求均由AIHubMix返回“账户余额不足”的HTTP 403，使用gpt-5.6-luna。它们不是阿里云调用。拒绝集中在上海时间18:16:39–18:16:51；原批量驱动预先提交全部任务，没有在首个永久账户错误后停止后续排队任务。不能将这些无完成报告的任务解读为28次业务推理错误，也不能将快速拒绝视为低时延优势。

另外3次进程中断的原因未证实，持久工具轨迹、旧库和未知费用预留均已恢复归档。全部33次未通过都保留在分母；不补跑、不把条件完成率替代主结果。用户随后确认AIHubMix已恢复，新的单次探测已成功；该探测不属于本轮144次。

后续工作台和新研究使用独立的provider_gate与有界排队驱动。在预算预留和HTTP请求前检查服务状态；认证、付款或明确余额错误会持久暂停该供应商。已发送的请求可以结束，下一次调用被本地拦下。其他供应商和本地GPU不受该暂停影响。它不修改本轮冻结方法，也不能追溯修复旧28次失败。

## 两份完成报告的证据缺口

| 运行 | 实际业务结果 | 缺口与依据 |
|---|---|---|
| shortage_alternative / bootstrap / single | 已实际选入有库存的替代，尚未批准 | 引用了起始O-1的insufficient_stock问题；当前选入后真正待处理的是substitution_requires_confirmation，未引用当前问题。 |
| shortage_alternative / neither / coordinator | 已实际选入替代，状态needs_clarification，无提案或确认 | 引用了替代对象与差异，但没有引用当前具体问题code。所有已引用字段真实，仍未覆盖预先要求的必要依据。 |

这两份报告不能描述成“虚构下单”或“错误批准”。它们通过业务状态检查，因必要来源覆盖不足而失败。

## 六次动作检查反馈

| 运行 | 第一次结束尝试 | 同次运行中的修复与限制 |
|---|---|---|
| combined / guard / on_demand | 修订后只引用费用，漏新版到达时间 | 补充当前提案arrival_at，未再创建版本，最终通过。 |
| product_info / guard / on_demand | 引用原始商品source_record/color中的Black | 检查器要求标准化variant/color，模型另加black字段后通过。原引用本来有颜色依据；这是路径要求过严带来的额外调用，不能算纠正了颜色事实错误。 |
| rule_blocked / both / coordinator | 引用规则与具体问题但漏当前订单状态 | 补充当前order_check/status及选择依据后通过。 |
| shipping_infeasible / guard / single | 无解路线使用了不匹配的决策状态 | 将状态改为unfulfillable，保留原约束和调整选项，最终通过。 |
| shortage_alternative / guard / on_demand | 已选入候选，漏当前待批准问题 | 增加当前issues引用后通过。 |
| shortage_alternative / guard / single | 漏当前状态和待批准替代差异的来源 | 增加当前status和differences后通过。 |

六次反馈最终都通过，但这种单次轨迹说明不能证明在另一次随机生成中必然获益；其中还包含一次可避免的严格路径修复。自由理由没有逐句做事实审核，当前工具约束也不证明候选全局最优。

## 成对变化如何解释

相对“两项关闭”，只有三条分数变化的左右两侧都返回了完成报告：单Agent仅起始读取在缺货案例上退步；协调员仅检查和两项开启在同一缺货案例上改善。其余变化至少一侧有账户故障或进程中断。只有combined场景的全部12配置均完成，因此不能删去故障后得到一份可靠的大样本对照。

按需组没有实际委派专家。当前证据支持“检查可以指出某些遗漏”和“检查也会增加不必要修复”，尚不支持“协作更优”或“v3显著提高准确率”。下一轮使用新订单状态、完整费用记录、供应商暂停和独立登记；不回填本轮失败结果。

## 原始证据

- [两份报告及全部144行读数](../evidence/apparel_state_pilot_v3/case_results.csv)
- [逐次配置变化](../evidence/apparel_state_pilot_v3/comparison.json)
- [原始结果清单和SHA](../evidence/apparel_state_pilot_v3/summary.json)
- [中断记录](APPAREL_STATE_V3_INTERRUPTION.md)
- [新服务恢复探测](../evidence/aihubmix_recovery_probe_20260908.json)
- [供应商暂停与有界排队检查](../evidence/provider_gate_and_report_tests_20260908.xml)

商品资料公开；规则、库存、运输和订单模拟；真实客户/用户仍为0。

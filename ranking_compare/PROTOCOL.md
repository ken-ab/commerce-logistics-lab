# 商品重排小规模模型对比

登记日期：2026-09-08。用户明确要求比较多个模型，并用小样本筛选提高商品匹配效果。本实验不改变原14,496查询最终排序结果，也不改变进行中的320次Agent最终复制。

## 输入与分区

从原ESCI query-group分区中选24条development、48条validation查询，US/ES/JP分别8/16条。排除原排序已用的全部15,696个query ID及query-group SHA；按新固定salt与query-group SHA散列顺序选样，每个group只选一个query ID。选样只读ID、语言、分区、query-group哈希与候选数量，不读取标签或模型效果。原测试分区不参与模型选择。

选样后，全部候选先用原固定Qwen3-Reranker-0.6B、product指令、BF16、512 tokens、batch16打分。商品文本沿用原标题、品牌、颜色、前1200字符描述。按分数降序、商品ID打破平局，取前10项进入大模型，剩余候选顺序保持不变。提供给大模型的10项按query/product哈希次序展示并映射为c01等临时编号，不显示原排名、分数、ESCI标签和商品ID。各模型得到完全相同的候选文字与展示顺序。

这是“0.6B预排 + 大模型重排前10项”的策略比较。大模型可能看到超过本地模型512-token截断范围的文字，因此不能把变化全部归因于参数规模；没有评估被0.6B排在10名以后的商品救回能力。全部候选仍参与最终指标计算。

## 模型与预算

初始候选：qwen3.8-max、deepseek-v4-pro、gpt-5.6-luna；0.6B和BM25作对照。用户提到的“Tiny”名称尚待明确，未在运行后临时替换成猜测的模型。

Qwen与DeepSeek使用同一北京DashScope账户。DeepSeek的模型ID、功能与价格已按阿里云当前官方页面核对，使用平台直供模型，不转发到另一家服务商。Luna使用原AIHubMix配置和费率。新费率表单独版本化，原冻结rate_card.json不改。

所有调用仍写同一evidence/api_budget.sqlite。新比较总核算及预留上限20元、开发（含服务校准）上限12元、验证上限8元，同时保留全项目300元授权和240元自动线。正式模型比较在原Agent最终复制结束后启动，避免占用其剩余预算与并发。无密钥的网络诊断不计为模型试验。

## 运行与评分

固定一个通用排序提示，不按模型或开发案例反复改写。只允许返回完整的临时编号排列。Qwen、DeepSeek采用非思考模式及固定输出上限；Luna保留其已验证的low推理设置。最终配置、代码与数据在首次正式开发调用前登记；服务能力校准只用人工小夹具，不使用开发或验证案例挑参数。

每个模型每条查询只做一次正式HTTP提交，不自动重新生成格式错误或不理想排列。传输失败、重复编号、漏项、额外编号、JSON格式错误均保留；实际可用策略退回原0.6B排名，并单列成功响应条件下的描述性分数，不隐藏失败率。永久账户或权限拒绝停止该模型后续调用，其余模型的运行范围按登记记录。

主指标NDCG@10，次指标Exact Top-1、MRR、费用、时延和有效排列率。保留原ESCI增益E=1/S=0.1/C=0.01/I=0；查询宏平均。开发主指标最高的可用候选进入验证，完全同分时依次按Exact Top-1、费用、模型ID打破平局。服务校准失败的候选标为未可用，不声称质量差。

验证仅运行预先选出的一个候选，比较同批48条0.6B基线，不用验证重新挑第二名。报告配对query-group bootstrap 95%区间及每语言结果；若提升证据不足、首位命中回退或服务不可用，保留现有方案，不把小样本分数冒充完整最终成绩。明确没有以此证明所有模型或所有参数设置中的最优模型。

## 来源

- 原方法：research/RANKING_PROTOCOL.md、ranking/model.py、ranking/metrics.py。
- Qwen：https://help.aliyun.com/zh/model-studio/qwen3-8-max。
- DeepSeek：https://help.aliyun.com/zh/model-studio/deepseek-v4-pro。
- 接口：https://platform.qianwenai.com/docs/api-reference/chat/openai-chat。
- Luna：原research/rate_card.json的已核查费率与来源。

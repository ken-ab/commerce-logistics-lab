# Kimi 强制推理的费用预留修正

本文件替代未通过真实校准的RECOVERY_V1实施路径；不覆盖其代码、冻结和失败记录。原100模型提示、模型ID、输出参数、推理参数、题目、评分与晋级规则保持字节不变。

## 新证据与决定

原Kimi K2.7 Code两次请求设置max_completion_tokens=1024，实际总输出4064和5650。随后唯一人工夹具校准换成max_tokens=8，返回finish_reason=length，但usage为141总输出，其中124推理token，不能证明总输出受8限制。仅更换参数的方案因此不启用，不重测原两道正式题。该失败校准核算0.005652元并保留。

阿里云官方说明Kimi K2.7 Code为仅思考模型，不能关闭思考，且不支持thinking_budget限制推理链长度；因此不得把requested reasoning_effort=none标为“实际无思考”。AIHubMix当前模型页及已冻结目录列出262144上下文、32768最大输出，但此次接口行为未能证明所有推理token都被较小输出参数包含。

后续仅修改费用预留：该模型每次按完整262144上下文长度作为输出token保守预留，另加原输入保守估计和20%余量；这是故意比目录32768输出上限更保守的费用界限。收到有效usage后按实际输入和总输出结算，推理token不重复收费。未知费用保留整个预留。这可能使100元下可完成的请求变少，不能以猜测超时免费来释放预算。

请求JSON仍与原客户端逐字节一致，包括max_completion_tokens=1024、reasoning_effort=none。所有158条原结果可沿用，含两条原Kimi结果和全部失败；只在派生汇总中从已结算账本补回旧异常遗漏的usage和费用，不改原文件。比较的是可调用的登记配置；Kimi的实际强制推理单独标注，不能声称100模型获得相同推理资源或这是各模型的最大能力。

## 执行与审计

入口model_selection_100.native_bound_run，结果目录evidence/model_selection_100/native_bound_v1。新登记绑定原504文件方法、新修正及原158条结果。原recovery_v1只有一次人工校准，没有新增正式请求。

全任务仍以model-selection-100前缀原子核算，100元总上限包括全部历史校准和失败；全项目480元上限不变，初筛及校准82元分配不变。解除当前校准失败暂停前须确认无并行选型进程、旧账本状态未变、修正已登记且费用预留检查通过。若实际费用超过新预留，继续沿用原子停止机制。不得无依据清零费用或重发有purpose的请求。

评分公式、24/60/150样本、独立验证门槛与初筛后费用复核流程沿用原PROTOCOL.md。记录原参数未限制总输出这一已知差异，不能以原文“固定1024上限”声称所有供应商均实际执行了同样的资源约束。

来源：[Kimi模型页及路由输出上限](https://aihubmix.com/model/kimi-k2.7-code)、[阿里云Kimi接口：仅思考模型与thinking_budget限制](https://help.aliyun.com/zh/model-studio/kimi-api-by-moonshot-ai)、[AIHubMix统一推理参数](https://docs.aihubmix.com/cn/api/unified-inference)。文档与真实usage共同支持此修正，不能据此确认一次请求究竟落到哪个上游供应商。

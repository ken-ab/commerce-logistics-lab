# 模型接入与人民币 300 元预算

2026-09-08补充：正式业务实验实际指定GPT-5.6-Luna（AIHubMix）生成，Qwen3.8-Max（阿里云）做报告审核；应用默认模型与早期配置别名不能替代运行记录中的显式模型。08:09原Qwen检查成功，08:11最终复制已恢复。最新配置与网络核对见[API诊断](QWEN_API_DIAGNOSIS_20260908.md)。以下保留首次接入历史。

2026-09-07：桌面 `.env.txt` 已规范化为项目根 `.env`，原文件保留。`.env` 是运行配置，Python 依赖环境位于 `.venv/`。密钥不写入报告、知识库或 Git；项目 `.gitignore` 已排除该配置。

| 用途 | 配置模型 | 当前实际验证 |
|---|---|---|
| 默认执行 | Qwen3.8-Max / DashScope | 模型目录与真实工具往返通过；完整流程出现连接中断，失败 trace 保留 |
| 低成本基线 | Qwen3.8-Flash / DashScope | 模型目录与真实工具往返通过；完整流程尚未通过 |
| 评判候选及备用执行 | GPT-5.6-Luna / AIHubMix | 真实工具往返通过；11 次模型调用完成选品、购物车、报价、模拟订单确认和幂等校验 |

备用执行通过不代表评判质量已经校准，也不代表默认 Qwen 配置已经改变。界面可以明确选择上述三个模型；每次运行保存所选及服务返回的模型名。

公開计费依据为 [Qwen3.8-Max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Qwen3.8-Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) 和 [AIHubMix Luna 模型页](https://aihubmix.com/model/gpt-5.6-luna)。费率、日期与单位核查记录在 `rate_card.json`，Luna 美元价格按 8 元人民币/美元保守预留；这不是汇率行情或已核销的账单。

所有生成、工具决定、评判、技能生成和显式重试必须走 `model_client.py` 及 `evidence/api_budget.sqlite`：先预留、后按 usage 结算；失败请求保留全部预留额。预算硬上限 300 元，自动执行上限 240 元，余量 60 元。客户端没有隐式重试，Agent 最多显式重试一次，每次分别预留和记录。

账本查询：`.venv\Scripts\python.exe -c "from research.model_client import BudgetedChatClient; print(BudgetedChatClient().ledger.summary())"`。金额是 token 推算加不确定请求预留，不等于平台账单。报告不得将连接失败记为零成本。

早期连接诊断记录 `evidence/dashscope_network_probe.json`。后续探测发现，同一 DashScope 域名的部分 DNS 地址握手超时，另一些可在 P-256 配置下建立 TLS 1.3。项目增加了发送 HTTP 前的 DNS 地址连接尝试；保持主机名与证书校验，证书错误立即失败，HTTP 请求提交后不在传输层重试。没有改变系统代理。

`evidence/qwen_tls_compatibility/` 保留超时、400参数错误、逐地址探测及修复后的真实成功调用。按[官方 Function Calling 文档](https://help.aliyun.com/en/model-studio/qwen-function-calling)，强制选择审核工具时使用非思考模式；普通 Agent 的自动工具选择保留原配置。Qwen3.8-Max 已实际成功完成审核探针，完整校准正在进行；完整 Agent 流程仍须另验。Luna 的完整运行记录为 `evidence/live_commerce_ad694783999b406c979d59159b684076.json`。

公开副本省略与本项目无关的私人配置诊断。早期工具选择兼容问题与失败调用不计入成功率分子。

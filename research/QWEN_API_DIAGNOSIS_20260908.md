# 千问 API 配置与超时诊断

2026年9月8日。URL、模型名称与主要审核参数符合当前官方文档。已复现本机连接不稳定，官方示例所用OpenAI SDK也出现连接超时。现有证据可以区分账户拒绝与连接故障，但不能确定网络根因属于本机、运营商、沿途设备或阿里云接入节点。

## 配置核对

| 项目 | 当前实际配置 | 核对结论 |
|---|---|---|
| Base URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 与用户提供的千问官方入门页相同；原域名仍受支持 |
| 实际HTTP路径 | `POST /compatible-mode/v1/chat/completions` | 正确，没有重复拼接`/v1`或`/chat/completions` |
| 审核模型 | `qwen3.8-max` | 官方有效调用ID；成功响应也返回此名称 |
| 鉴权 | 原配置中的DashScope密钥，Bearer形式 | 已有同一地址、同一密钥的成功调用；未展示或变更密钥 |
| 流式输出 | `stream=true`，`include_usage=true` | 符合流式协议；末尾用量用于费用估算 |
| 强制审核工具 | 指定函数，`enable_thinking=false` | 符合强制函数调用对思考模式的要求 |
| 输出限制 | `max_completion_tokens` | 当前模型专用API参考明确支持；未因旧概览的概括性说明而改成另一套参数 |
| 业务任务生成 | `gpt-5.6-luna`，AIHubMix | 与Qwen审核接口分开；最终运行config.json明确指定Luna |

来源：[用户提供的官方入门页](https://platform.qianwenai.com/docs/developer-guides/getting-started/introduction)、[Chat API参考](https://platform.qianwenai.com/docs/api-reference/chat/openai-chat)、[强制函数调用](https://platform.qianwenai.com/docs/developer-guides/tool-calling/function-calling)、[流式输出](https://platform.qianwenai.com/docs/developer-guides/run-and-scale/streaming)。

文档存在一处需要区分的表述：通用兼容概览将`max_completion_tokens`列入忽略项，但当前Chat API参考与模型相关参数说明明确列出Qwen3.7-Max之后支持。此次SDK输出上限验证未成功连接，因此没有得到新的运行时上限验证，不能把它写成已通过。当前原客户端仍沿用此前登记的模型专用接口契约。

## 两类故障的时间线

1. **05:13之前：账户拒绝。** 原审核日志收到HTTP 400，消息为账户状态拒绝，并指向阿里云的overdue-payment文档。这是服务端返回的错误，不是本地超时。未读取平台账单，无法判断是欠费、充值同步延迟、密钥所属账号不同或账户异常。
2. **07:58检查：响应头超时。** 0.813秒完成TLS握手，90.86秒后在`response_headers`阶段超时，尚未收到HTTP状态码或响应体。这条记录不能被称为“又欠费了”。
3. **08:09检查：服务成功。** 账户状态恢复后，原模型正常返回工具调用；13.172秒，估算0.004236元。这不能证明账户状态是唯一恢复原因。公开副本省略私人充值与账户余额信息，保留技术诊断和运行记录。
4. **08:11续跑之后：成功与偶发超时并存。** 截至08:21:45，最终复制完成79/320条；恢复后审核传输50次HTTP完成，1次响应头超时、2次HTTP 200后响应体超时，另1次进行中。完成请求约1.984至23秒。这里只描述当时的传输快照，不是最终任务成功率。

阿里云[错误说明](https://help.aliyun.com/zh/model-studio/error-code#overdue-payment)要求：未欠费时核对API Key所属账号，仍异常则联系客服；充值后可能存在余额同步延迟。不能仅用控制台“预估应付”数字判断API Key所属账号是否欠费。

## 不带密钥的连接对照

08:16对该域名当时DNS返回的4个地址，分别采用Python默认OpenSSL、Python P-256、Windows curl/Schannel做1次连接，共12次。全部保持证书与主机名校验；不使用密钥，不调用推理，只请求模型目录并观察HTTP响应。

| 地址 | Python默认TLS | Python P-256 | Windows curl |
|---|---|---|---|
| 8.152.159.24 | 0.250秒收到401 | 0.250秒收到401 | 4.521秒收到401 |
| 8.140.217.18 | TLS握手超时 | TLS握手超时 | 0.237秒收到401 |
| 39.96.213.166 | TLS握手超时 | TLS握手超时 | 建立连接超时 |
| 39.96.198.249 | 0.375秒收到401 | TLS握手超时 | 建立连接超时 |

这里的401是故意不带密钥后得到的正常鉴权挑战，只证明传输收到了服务端响应，**不是用户密钥失效**。每种组合只有一次尝试，运行时间不完全相同，不能据此永久屏蔽某个IP，或断言某TLS库总是更快。

这组结果说明连接故障可在完全不做推理、完全不读取账户余额时出现。P-256也不是通用修复；原评测的有界DNS尝试和连接复用只能缓解部分故障。此次没有修改冻结传输代码、系统代理、TLS校验或DNS。

## 官方示例所用SDK的对照

本机OpenAI SDK 3.8.0、httpx 0.28.1，使用同一地址、同一密钥和Qwen模型；显式直连，默认TLS校验，10秒连接/90秒读取超时，关闭隐式重试。一次独立的小请求在约11.109秒失败，异常链为`APITimeoutError → ConnectTimeout`，没有HTTP状态码与Request ID，未到完成输出阶段。

请求原本同时观察`max_completion_tokens=64`的输出限制，但由于连接失败，输出限制是否生效没有测得。没有重抽任何正式评测任务；本次0.20元费用预留保留，平台是否实际计费未核销。

安装在本机的SDK默认读取等待为600秒、默认重试2次。项目审核连接的读取等待为90秒，正式审核按原协议最多有限重试一次。因此“另一个客户端没有报错”可能来自更长等待、自动重试、复用连接或不同网络路径；尚未获得用户另一个软件的名称与配置，不能把这些可能性当作那个软件的已知行为。

系统和进程环境均指向`127.0.0.1:7890`代理，本次检查没有发现该端口监听。但正式审核由已登记的直连socket发送，SDK对照也设置`trust_env=false`，这些超时不能直接归因于该失效代理。依赖库访问远程价格表时的代理连接拒绝是另一条日志；本实验使用已经冻结的本地费率表。

## 可继续核对的方向

- 当前官方还推荐北京业务空间专属域名：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。原域名仍可使用。项目配置中没有业务空间ID，尚未做该地址的对照；不能声称迁移一定解决本次问题。见[官方接入地址说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。
- 对照用户实际能稳定调用的软件及其Base URL、模型和代理路径。无需在聊天中提供密钥。
- 后续连接实现应保留接入阶段、HTTP状态、Request ID与读取阶段的区分。只有服务端日志或请求追踪补足证据后，才可能继续定位响应等待卡在何处。
- 当前正式实验按原登记继续运行。最终样本、旧失败、评分和费用上限都已固定；不会把这轮诊断的成功探针计入任务成绩。

## 本地证据

- `evidence/provider_recovery/20260907T235828329690Z/result.json`及其transport：07:58超时。
- `evidence/provider_recovery/20260908T000923090285Z/result.json`：08:09原服务成功。
- `evidence/qwen_connectivity/20260908T001602534987Z/result.json`：12次无密钥连接对照。
- `evidence/qwen_sdk_diagnostic/20260908T002038105810Z/result.json`：官方SDK式请求与连接失败。
- `evidence/replication_recovery_launch_20260908T0010.json`：原最终实验的恢复登记。
- `research/diagnose_qwen_connectivity.py`：无推理连接检查程序。

本报告不包含API Key、访问口令或平台账单。账本中的累计金额是两家接口的成功调用估算加不确定请求预留，不能作为阿里云实际应付金额。

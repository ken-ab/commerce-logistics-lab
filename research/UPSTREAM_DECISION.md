# 上游选择与实际复用

核查日期：2026-09-07。Star 是当日快照，不能证明效果或维护承诺。

| 仓库 | Star 快照 / 近期更新 | 许可与适用范围 | 决策 |
|---|---|---|---|
| [anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) | 2,310 / 2026-09-01 | Apache-2.0；商家与购物 Agent、后端接口、工具、事实约束、记忆和展示。README 同时声明参考实现不保证持续维护 | 主要接口与可信工具组件基础；实现自己的持久化后端和付费调用适配 |
| [NVIDIA Retail-Agentic-Commerce](https://github.com/NVIDIA-AI-Blueprints/Retail-Agentic-Commerce) | 74 / 2026-08-24 | Apache-2.0；ACP/UCP、多服务、搜索与售后；部分数据另有许可 | 功能与协议参考，当前单机不部署完整 GPU 服务栈 |
| [Azure postgres-agentic-shop](https://github.com/Azure-Samples/postgres-agentic-shop) | 105 / 2026-09-02 | MIT；LlamaIndex、Postgres/向量/图、记忆与可观测性 | 数据与观测参考；未授权另行开通云基础设施 |
| [nitin27may/e-commerce-agents](https://github.com/nitin27may/e-commerce-agents) | 23 / 2026-09-04 | MIT；多 Agent、Next.js、FastAPI、Postgres 与 Redis | 保留前期基线源码和 35 项局部验证；不将其称为高热度方案 |
| [multi-agent-ecommerce-system](https://github.com/bcefghj/multi-agent-ecommerce-system) | 559 / 2026-04-05 | 仓库元数据未显示许可 | 不作为复制代码的主基础 |

主要固定提交：`anthropics/commerce-agents@fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`。上游保持原样，项目代码放在 `commerce_lab/`。已复用 `StorefrontBackend`、购物领域数据类型和 `Fence`，用公开商品目录替换 ACME 样例，并接入共享预算的 OpenAI 兼容调用层。`commerce_lab/backend.py`实现接口，`commerce_lab/agent.py`使用上游类型及工具记录边界包装；实际状态约束由本项目代码核验，包装文字本身不是安全保证。模型运行协议的改动属于本项目适配，不声称原版已经支持当前 Qwen 流程。

上游原版在本机安装后：补充 Windows 所需 `tzdata` 后，完整测试 **1,103 通过、1 失败、1 跳过**；剩余失败是 POSIX 文件权限 `0600` 的 Windows 断言。详见 `evidence/commerce_agents_windows_tests.xml`。这不能写成上游全测通过。

真实目录来自 [Amazon ESCI](https://github.com/amazon-science/esci-data)，已核验两个 Parquet 的 SHA-256。实际包含 1,814,924 条商品、2,621,288 条标注。目录无价格、库存、订单或用户账户；业务状态已独立标为合成研究环境。

评测候选：[τ-bench 当前 τ³ 版本](https://github.com/sierra-research/tau2-bench)、[DeepEval](https://github.com/confident-ai/deepeval)。须保存版本及协议；本项目定制任务不能冒称官方排行榜成绩。

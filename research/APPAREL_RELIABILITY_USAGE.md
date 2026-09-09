# 提案复核：本地使用与证据

新版接入只覆盖“复核当前提案”。启动时会检查完整的 144 次实验、独立审计、预登记门槛与文件散列；检查未通过会拒绝启动。是否实际接入见 [界面回执](../evidence/apparel_reliability_browser_20260909.json)。

在本地工作台选择已有订单，先检查当前选择、已批准的差异、预算和交期，再选择“复核当前提案”。页面会传入当前提案标识。保留或修订提案时，Agent 读取商品材料、库存、事件和旧方案，并接受原有的状态与依据检查。更换明确要求或最终确认仍是单独的用户操作。

完成后，“新旧班次与事件核对”显示程序根据已记录数据生成的说明，包括原班次身份、相关事件、新班次以及等待时间。原班次延误与改乘另一班次分开说明。完整结果中保留模型补充解释，但它不属于该程序事实检查的保证范围。旧版历史结果没有这段说明时，不会替它补造。

三种执行策略继续可选，业务模型固定，方便比较同一操作。具体收益、费用及失败见 [本轮六组结果](APPAREL_RELIABILITY_RESULTS.md)。报告中的 24 个状态是开发者编写的模拟业务状态，包含只读对照；不能把验收率称为真实商家准确率。检索 NDCG、首位命中与 100 元模型选型表仍保持独立口径。

当前新版入口为 [start-apparel-reliability-parallel.ps1](../start-apparel-reliability-parallel.ps1)，服务地址为 `http://127.0.0.1:5177/`。原工作台仍在 `http://127.0.0.1:5176/`，原草稿与待确认提案保留。两个地址的浏览器会话按端口隔离，新地址不会自动显示旧会话的草稿；没有复制会话凭据。

自动审批审查阻止了停止旧服务并重启的命令，仅返回 `blocked by policy`。因此启用了上述并行入口，没有再次尝试终止旧进程。原 [start-apparel.ps1](../start-apparel.ps1) 和需要端口空闲的 [start-apparel-reliability.ps1](../start-apparel-reliability.ps1) 保留；当前运行状态以界面回执中的地址为准。

开发与核验材料：

- [预登记与门槛](APPAREL_RELIABILITY_PROTOCOL.md)、[24 个状态与执行顺序](../evidence/apparel_reliability_study_v1/registration.json)。
- [独立审计](../evidence/apparel_reliability_audit_20260909.json)，逐例核对原始 SQLite、引用、路线、事件、方案版本和账本。
- [21 项核心逻辑检查](../evidence/apparel_reliability_tests_20260909.json)，以及 [13 项适配检查与显示检查](../evidence/apparel_interactive_reliability_final_tests_20260909.json)。离线检查使用脚本响应，没有新增模型费。
- [启动前业务表快照](../evidence/apparel_reliability_ui_before_20260909.json)。最终回执比较启动前后全部八张业务表，不根据截图推断数据库未变化。

已完成的首次实验与报告使用独立文件保存；不要重复执行付费 runner 来覆盖它们。后续修改需要新版本、新的实验登记，并保留本轮失败与限制。

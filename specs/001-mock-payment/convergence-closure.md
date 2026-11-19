# 收敛任务收口：初始化设计归属与验收映射勘误

日期：2026-11-20。范围：T083、T084。

本记录只补齐既有实现的设计归属与证据引用，不改变业务规格、不修改应用或工作流，
不授权真实初始化、包创建、可见性修改、发布或部署。当前负责人已要求从最新main
建立分支完成两项记录；这不是重新申请此前已经获得的初始化实现与公开风险设计授权。

## T083：初始化入口的设计归属

**结论：作为本特性的补充交付恢复能力维护，独立于正常配对发布，不新增支付业务需求。**

原计划的发布链路负责交付已验收的app/migrate镜像对；后来发现迁移包尚不存在，
正常发布按既有门禁拒绝。负责人已批准独立初始化入口，并明确接受首次推送可能直接
公开的设计。这些既有决定及具体操作约束见
[迁移包一次性初始化](../../docs/migrate-initialization.md)。
本次将该补充决策关联回[技术计划](plan.md#补充交付恢复能力迁移包一次性初始化)，
不把后续决定伪装成原规格已有的FR。

| 项目 | 已有实现及归属边界 |
| --- | --- |
| 工作流入口 | `.github/workflows/release.yml`的`initialize-migrate` job；默认仍为`publish`，两个写入job互斥 |
| 执行模块 | `scripts/init-migrate-package.py`；不调用正常publish来绕过缺包门禁 |
| 来源 | 消费同一源码SHA的可信main CI原始bundle，绑定run/attempt及artifact身份，不重新构建 |
| 固定目标 | 仅`ghcr.io/xtianxx/payment-hub-migrate`，程序计算`init-<source_sha>`；不推送app |
| 初始化前提 | app原public关联包有效且身份不变，migrate确认缺失；独立确认初始化授权、前提及首次可能公开风险 |
| 实际可见性 | 观测private/public，不自动修改；不能保证首次先private，也不能撤销已经发生的公开 |
| 剩余竞态 | 包复查、最终CI复查与push非原子；维护窗口须排除其他写者，不能承诺标签原子首次创建 |
| 结果 | 摘要始终`published=false`，初始化已验证不等于完整镜像对已发布 |
| 失败处置 | 保留未知副作用和清理状态，不自动重试、删除、公开化或转入正常发布 |

正常发布仍要求两包均已存在、为public、归属及关联正确，并完成原镜像对按digest
拉回及运行验收。T078的真实外部发布验收仍是原任务，不由T083、初始化代码存在、
本次文档收口或CI绿灯代替；部署也仍是另一个明确授权动作。

## T084：US1-A3验收映射勘误

这是**独立勘误索引**，不是对历史运行的重新验收。原始记录保持不变：

- 文件：`artifacts/phase6/final-validation/run-20261112T033948Z-67d8d604/acceptance.json`。
- 原记录schema：`payment-hub.phase6.local-acceptance.v1`。
- 原记录UTC：`2026-11-12T03:58:52.451595+00:00`。
- 原文件SHA-256：`c83aca1f1e9aa334cb7cf0563da5886a9a5acda81504779d8f826457ce7dd371`。
- 原源码清单摘要：`6aebe97c6a09dae33c6df21f1029100794f3b744c539c761370da6fb6859538b`。

| 源引用 | 原字段 | 更正后的主证据 |
| --- | --- | --- |
| spec：US1/AC3；T082 | `original_17_scenarios[id=US1-A3].root = TestUS1HTTPBoundary` | `tests/us1_test.go`中的`TestUS1Payment/failed` |

更正依据来自现有源码断言：

1. `TestUS1Payment/failed`明确传入`DirectiveFailed`和`ResultFailed`，
   经`us1RunPaymentScenario`分别按平台号、业务单号查询，核对两者均返回原确定失败订单。
2. `us1VerifyStoredFacts`同时核对订单、Mock交易及处理记录，要求三类行数各为1；
   `us1FinalizeScenario`核对独立付款调用计数及观察器可信封口，预期付款次数为1。
3. `TestUS1HTTPBoundary`负责请求大小、编码等HTTP边界，不是该确定失败场景的主证据。
4. 失败后**重复提交**不重付属于US2的补充佐证：`tests/us2_replay_test.go`的
   `TestUS2Replay/failed`两次重放均要求原FAILED订单不变，并以
   `Payment: 1, Query: 0`封口。它不是此次替换US1-A3的主映射，也未在本次重新执行。

原运行的`result`、原始报告、其他场景和历史TDD缺证均不被改写。
原始`artifacts/`受忽略规则保护，不把整包或敏感原始产物纳入版本控制；
本勘误保存必要路径、摘要与定位信息，原文件缺失时应报告不可复核，不伪造重建。

## 完成判断与验证边界

T083完成的是设计归属及边界记录，T084完成的是证据索引勘误。
两者都不代表重新执行真实PG、HTTP、重启、Docker或GHCR验收，
也不把历史输入的运行结果提升为当前源码的全量验收结论。

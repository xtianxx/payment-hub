# Problem Definition: 探流（FlowProbe）——概念验证MVP的负载表现缺少证据

- **Slug**: flowprobe
- **Created**: 2026-11-13
- **Inputs used**: research.md；用户关于概念验证MVP、负载测试与瓶颈识别的陈述及澄清。定义时由research.md的I1引用原intake；随后按用户要求统一目录，原始记录现位于同目录`intake.md`。
- **定义边界**：将MVP作为已完成概念验证的研究对象，不重新打开功能或发布验收；当前没有证据证明它已经出现性能故障。以下定义不构成测试执行、安装、优化或资源变更授权。[source: research.md，Users & Demand、Market & Context]

## Problem Statement

项目开发者在概念验证MVP完成后，希望了解它在负载下的表现，但现有功能、幂等并发及重启验证没有给出对应负载条件下的吞吐、延迟分布和资源表现，因此尚不能基于实测解释性能边界或判断瓶颈在哪里。现在需要解决的是性能认知与归因证据的缺口，而不是预先认定数据库、连接池或某段代码必须优化。[source: research.md，Prior Art、Data & Constraints；用户需求U1]

## Affected Users & Stakeholders

- **Users**：项目开发者（提出本想法的用户）——希望理解负载变化与系统表现的关系，目前无法从既有正确性测试得出容量或瓶颈结论。[source: research.md，Users & Demand、Prior Art]
- **Stakeholders**：同一项目负责人——决定探索范围、资源与执行授权，并判断证据是否足以支持后续优化讨论。这里是同一人的不同职责，不推定另有运营团队、付费客户或生产用户。[source: research.md，Users & Demand，I2]
- **外部影响范围**：当前没有外部客户压力、线上事故或业务损失数据，不能把本问题描述为生产用户已受性能故障影响。[source: research.md，Users & Demand、Evidence Against the Idea]

## Goals

1. **获得可解释的负载表现认知**：能够说明在什么业务行为、数据规模和资源条件下，实际吞吐、响应时间及错误表现如何，而不只得到一个脱离条件的RPS数字。[source: research.md，Data & Constraints、E2]
2. **形成有证据的瓶颈判断**：能够区分观察到的现象、可能原因及尚未证实的假设；如证据不足，应明确“未能定位”，而非强行指定一个瓶颈。[source: research.md，Data & Constraints、Evidence Against the Idea]
3. **保留业务语义的解释边界**：负载下仍分别理解HTTP结果、付款结果、UNKNOWN、issues及幂等效果，不把2xx比例或吞吐增长直接等同于业务正确与追踪完整。[source: research.md，Data & Constraints，I7、I16]
4. **支持后续有依据的讨论**：使项目负责人能判断是否存在值得进一步研究或优化的问题；本阶段不预设一定需要优化，也不决定优化方向。[source: 用户需求U1；research.md，Market & Context、Gaps & Open Questions]

## Non-Goals

- 不默认开展超出容量极限、测试至崩溃的压力测试；本想法已明确为负载测试。[source: research.md，Users & Demand]
- 不认证生产级SLA、高可用或真实支付渠道容量，不从同进程、共享数据库的Mock结果外推真实资金链路表现。[source: research.md，Market & Context、Evidence Against the Idea]
- 不以引入某个负载工具、监控平台或新架构作为成功条件；不在本定义中选工具、设计API、数据模型、实现任务或测试方案。[source: research.md，Prior Art、研究范围]
- 不在没有测量依据前认定连接池、行锁、数据库或HTTP处理就是瓶颈；不预先承诺代码优化或性能提升幅度。[source: research.md，Data & Constraints]
- 不为得到更高吞吐而改变既有金额、幂等、确认保护或超时语义；不把功能正确性与性能表现相互替代。[source: research.md，Data & Constraints，I7、I12、I16]
- 不扩大到真实资金、生产数据、GHCR发布或部署，也不解除数据隔离与凭据保护要求。[source: research.md，Data & Constraints，I7、I15]

## Success Metrics

以下是从已有需求与研究提炼的**衡量维度**，不是已批准的容量目标或执行方案。具体负载、样本充分性及数值阈值仍待确认；现有业务超时上限不作为p95/p99成绩或性能达标线。[source: research.md，Data & Constraints、Gaps & Open Questions]

| 衡量维度 | 如何判断问题得到更清楚的回答 | 当前基线 / 未确定项 |
|---|---|---|
| 结果的条件完整性 | 每项性能结论能对应明确的业务范围、数据规模、环境资源、负载与观察时段；缺失项明确列出 | 尚无可引用的专门负载基线；所需条件的具体值待确认。[source: research.md，Prior Art、Data & Constraints] |
| 负载与表现可区分 | 分别记录或解释预期负载、实际发出请求、成功吞吐、延迟分布与错误/超时，不把这些量合并为一个“QPS” | 当前RPS、时延分位数和错误率均未知；是否设通过阈值待确认。[source: research.md，E2、Data & Constraints] |
| 归因证据覆盖 | 每个声称的瓶颈都有相应现象与支持证据，并披露客户端受限、共享资源或测量偏差等尚未排除的解释；无法归因时明确不足 | 当前没有已识别、已实测证实的瓶颈；可获得证据及充分性标准待确认。[source: research.md，Data & Constraints、Evidence Against the Idea] |
| 业务语义可辨识 | 业务结果、UNKNOWN/issues、HTTP错误与幂等效果能够分开解释；观察到重复付款或确认结果被破坏时，不能因吞吐较高判为成功 | 已有对应正确性约束与功能验证，不存在本次负载下的实测结论。[source: research.md，I3、I5、I7、I16] |
| 认知缺口是否缩小 | 能够清楚回答“哪些负载条件已观察、哪些现象已解释、哪些仍未知”，供负责人审阅 | 定性维度：用户当前只有探索意愿，无已给定的性能目标或优化排序。[source: research.md，Users & Demand、Market & Context] |

不要求本次一定发现性能缺陷；“在已观察条件下没有足够证据证明瓶颈”也可以是诚实结论，但不能扩写成“系统不存在瓶颈”或“已满足生产性能要求”。[source: research.md，Evidence Against the Idea；ASSUMPTION：探索的价值在于减少未知，而非必须找到缺陷]

## Cost of Inaction

如果不开展这项探索，已有功能验证仍然是其所覆盖范围内的证据，但项目在不同负载条件下的表现与瓶颈仍缺少可引用的测量依据。负责人将无法依据本次尚不存在的数据讨论容量或安排性能优化；目前没有证据可量化用户损失、收入损失或延误成本，也不能声称不开展负载测试会使MVP功能失效。[source: research.md，Prior Art、Market & Context；ASSUMPTION：缺少实测会限制性能判断]

## Open Questions

- [NEEDS CLARIFICATION: Q1 需要理解哪些业务行为及其比例：新订单、重放、冲突、已确认查询、UNKNOWN核实，还是其中一部分？]
- [NEEDS CLARIFICATION: Q2 结论针对哪个运行环境，应用、数据库与负载发起端的实际CPU、内存、磁盘、网络条件和资源边界是什么？]
- [NEEDS CLARIFICATION: Q3 初始数据规模、订单分布及缓存/连接等初始状态如何界定，哪些条件需要保持可比较？]
- [NEEDS CLARIFICATION: Q4 希望考察什么请求到达或用户交互行为、负载范围和观察时长，是否有现实场景依据？]
- [NEEDS CLARIFICATION: Q5 目标仅是观察趋势与定位瓶颈，还是要对明确的吞吐、分位时延与错误率要求作达标判断？若有，数值与依据是什么？]
- [NEEDS CLARIFICATION: Q6 可接受的资源占用、数据增长、错误表现及停止边界是什么，实际运行、安装或资源变更有哪些授权限制？]
- [NEEDS CLARIFICATION: Q7 本轮是否仅识别和解释瓶颈，后续优化验证及其范围是否需要另行确认？]

来源标识沿用同目录`research.md`的Sources；本文未新增外部资料或重新抓取网页。

# Idea Research: 探流（FlowProbe）——概念验证MVP负载测试与瓶颈探索

- **Slug**: flowprobe
- **Created**: 2026-11-13
- **Evidence confidence (overall)**: medium
- **研究输入**：用户确认以`flowprobe`研究“对概念验证MVP开展负载测试，观察负载表现并识别瓶颈”。研究启动时由用户确认补足输入，随后按用户要求将原intake迁入本目录；原想法及“压力测试→负载测试”澄清见同目录`intake.md`。[source: U1, I1]（confidence: high；cited）
- **范围**：本记录只收集事实、限制和反向证据，不作可行性裁决、工具选型、负载方案或优化设计。未运行负载测试，未安装工具，未启动应用或数据库。[source: R0]（confidence: high；cited）

## Users & Demand

- **直接需求信号**：项目用户明确希望观察负载表现、找瓶颈，并纠正为“负载测试”；这属于用户陈述，不是已观察到的线上拥塞或容量事故，也不自动意味着需要突破容量极限或测试至崩溃。[source: U1, I1]（confidence: high；cited）
- **项目使用背景**：宪章将项目目标描述为求职展示、系统学习与个人开发、AI辅助，初期仅使用Mock Provider、不处理真实资金。这解释了当前研究的上下文，但不证明存在外部客户需求。[source: I2]（confidence: high；cited）
- **需求强度限制**：当前输入没有提供用户数量、生产流量、故障工单、用户访谈或业务损失数据。因此只有一位项目用户的明确探索意愿，尚无真实流量压力的需求证据。[source: U1, I1]（confidence: medium；cited，限已提供材料）

## Prior Art

### 项目内已有工作

- **并发正确性先例**：现有US2测试从两个真实HTTP请求出发，通过PG锁等待者计数核对并发和幂等。它直接关注同单、单次付款与持久效果，不是持续负载的吞吐或延迟基准。[source: I3]（confidence: high；cited）
- **演示客户端先例**：`scripts/demo.py`定位为单次固定五步HTTP流程；它是已有业务操作样例，不能据此声称项目已经具备负载发生器或容量报告。[source: I4]（confidence: high；cited）
- **既有验收证据**：交付审查修复记录包含790项Python、默认56项、race和静态检查等结果；该记录没有给出负载条件下的RPS、延迟分布或资源占用结论。本研究未重新执行这些验证。[source: I5]（confidence: high；cited）
- **检索边界**：本次对业务代码、`scripts/`、`tests/`、`deploy/`和工作流作只读文件名/内容检索，关键词包括`Benchmark`、`-bench`、load/perf/stress、负载/性能及常见发生器名称；未找到可直接引用的专门负载测试脚本和容量基线。不能据此排除仓库外、本地未纳入检索的实验或记录；本地配置文件的存在也不等于测试已经执行。[source: R1, I3–I5]（confidence: medium；cited）

### 公开先例：仅核实能力声明，不评选工具

| 先例 | 本次实际读取的证据 | 对本想法的证据意义 | 质量 |
|---|---|---|---|
| Grafana k6 | 仓库README声明可配置负载、脚本化行为、统计输出；文档索引列出open/closed models等场景模型 | 公开项目已把“业务行为、负载模型、统计结果”作为独立关注点，但未在本项目验证其适用性或成本 | [source: E1]；confidence: high（声明存在）/ medium（适用性）；cited |
| Vegeta | README定位为恒定请求率的HTTP负载工具，可作为CLI或Go库；报告区分请求发出速率、成功吞吐、延迟分位数和状态码 | 说明“设定速率、实际请求速率、成功吞吐”不是同一个数字；这是工具文档证据，不是Payment Hub的实测结果 | [source: E2]；confidence: high（文档内容）；cited |
| wrk2 | README讨论恒定吞吐、按请求原定发送时间计算延迟，以及coordinated omission（协调遗漏） | 提供测量失真的公开先例：只在收到答复后继续发送的模型可能少观察到慢时段；是否影响本项目尚未测量 | [source: E3]；confidence: high（原文论述）/ medium（项目适用性）；cited |

- **不能搬用外部数字**：上述README中的演示请求率、延迟、运行时长和阈值属于外部示例，不是本项目目标或能力依据。k6/Vegeta的能力描述主要来自项目自身，wrk2文中的部分比较明确涉及旧版执行模型，不能转写为2026年所有工具的对比结论。[source: E1–E3]（confidence: high；cited）
- **法律资料边界**：k6 README声明AGPL-3.0许可；本次未选工具、未评估具体使用或分发方式的法律义务，也没有其他合规判断。[source: E1]（confidence: high（许可声明）/ low（项目法律适用性未评估）；cited）

## Market & Context

- **当前上下文不是生产SLA补验**：规格明确不承诺生产级SLA、高可用或规模指标；计划也明确不设未经验证的吞吐和延迟目标。因此不能把性能目标尚未定义直接认定为原MVP的功能缺陷。[source: I6]（confidence: high；cited）
- **已有替代信息来源**：当前项目可引用功能、并发幂等、重启与故障测试判断相应正确性；这些证据没有建立负载容量。公开负载工具则说明已有通用替代品可供后续评估，并不证明一定需要新增自研工具。[source: I3–I5, E1–E3]（confidence: medium；cited，适用范围推论）
- **不开展研究的代价尚不能量化**：若没有负载数据，对容量与瓶颈的判断仍缺实测依据；但现有资料不能证明会造成多少损失或研发浪费。[source: I5, I6；ASSUMPTION：缺少实测会限制性能判断]（confidence: medium（信息限制）/ low（实际代价））
- **市场证据缺口**：没有取得竞品采用率、工具市场份额、付费需求或行业趋势数据；不把开源项目自述、知名度或示例数字当作本项目需求证明。[source: R0, E1–E3]（confidence: high（本次研究范围）；cited）

## Data & Constraints

### 已有业务与资源边界

| 事实 | 当前可确认内容 | 来源与置信度 |
|---|---|---|
| 业务范围 | 单演示接入方、单Mock、普通订单一次性全额支付；CNY整数分1–1,000,000 | [source: I7]；high；cited |
| 操作不是同一条路径 | 新单登记后调用Mock并保存观测；相同请求复用不再付款；已确认查询直接返回，UNKNOWN查询才向Mock核实并保存观测 | [source: I8]；high；cited |
| 共享资源 | 平台存储与Mock在同一应用进程，共用一个runtime pgxpool；Mock仍使用独立表和独立事务 | [source: I9]；high；cited |
| 连接池配置 | 应用通过`pgxpool.ParseConfig`创建配置，没有显式覆写MaxConns/MinConns等池字段；连接串只允许sslmode/search_path参数，不能把任意pool参数视作已支持配置。实际有效池上限与运行时环境尚未取证 | [source: I10]；high（代码）/ unknown（运行值）；cited |
| 数据库同步点 | 登记受业务唯一键约束；观测保存使用Read Committed、订单行锁、条件更新及同事务插入处理记录 | [source: I11]；high；cited |
| 超时是边界，不是成绩 | 主处理10秒、单Mock调用3秒、完整DB操作/事务2秒、最多一次2秒有限收尾；HTTP读5秒、写20秒；这些不是p95/p99实测或吞吐指标 | [source: I12]；high；cited |
| 测试模式有额外路径 | 正常app为test mode off；app-test启用同步私有UDS观测/屏障。两种模式的测量环境不能无说明地视为相同 | [source: I13]；high（配置）/ medium（测量可比性推论）；cited |
| 资源限制未完整描述 | 已读Compose文件没有CPU/内存资源配额字段；没有本次目标环境的实际CPU、内存、磁盘、PG有效配置和负载发生器资源记录，不能推导容量上限 | [source: I14, R1]；high（声明配置）/ unknown（实际环境）；cited |
| 数据与凭据限制 | 原设计要求开发/测试/演示隔离；日志和产物不得包含密钥、Authorization、DSN、完整报文或原始SQL。负载测试想法本身不解除这些约束 | [source: I15]；high；cited |

- **业务成功与HTTP成功不能混同**：规格允许UNKNOWN，也区分FAILED；计划明确2xx不能证明付款成功或追踪完整。Vegeta README的默认Success比例按HTTP状态/传输错误定义，不能直接替代本项目的付款、幂等与issues语义。[source: I7, I16, E2]（confidence: high；cited）
- **不同操作比例尚未知**：是否以新订单、相同订单重放、热点冲突、已确认查询或UNKNOWN核实为主，输入没有给定。由于代码走不同路径，不能用尚未定义的混合请求总量代表所有业务容量。[source: U1, I1, I8；ASSUMPTION：操作比例会影响结果可解释性]（confidence: medium）
- **瓶颈尚未识别**：共享池、行锁、唯一键、HTTP处理或客户端发生器都不能仅凭“代码里存在”就判为瓶颈。当前没有排队、锁等待、CPU、I/O或请求时延的对应实测证据。[source: I5, I9–I14；ASSUMPTION：归因需对应运行证据]（confidence: medium）

## Evidence Against the Idea

这里记录限制本想法价值或限制结果解释的证据，不作去留决定。

1. **缺少外部压力的观测信号**：当前只有项目用户的探索意愿，没有提供真实用户量、拥塞事故或容量不足损失；不能证明现在就存在必须解决的性能问题。[source: U1, I1]（confidence: medium；cited）
2. **原范围没有生产性能承诺**：如果把负载测试扩成生产SLA认证或真实支付容量声明，会超出当前规格；本次需求并未提供这种扩展依据。[source: I6, I7]（confidence: high；cited）
3. **Mock边界限制外推**：同进程、共享PG的Mock不代表真实渠道网络、独立故障域或资金事务，即使得到本地数字也不能直接宣称真实支付渠道容量。[source: I9；plan.md:164–168]（confidence: high；cited）
4. **测量方法可能制造错误归因**：wrk2说明协调遗漏和发生器自身资源/脚本开销的影响；Vegeta也警告无约束请求率/worker可能耗尽发生器资源。服务端变慢或客户端受限不能仅由一个总RPS数字区分。[source: E2, E3]（confidence: high（文档警示）/ medium（项目风险）；cited）
5. **已有正确性测试不构成性能基线**：两个并发请求和固定五步客户端能证明相应功能，却没有持续负载、资源消耗或延迟分布证据；复用其“通过”标志不能回答瓶颈问题。[source: I3–I5]（confidence: high；cited）
6. **目标与环境尚不明确**：没有明确业务混合、数据规模、有效资源和观察目标，当前无法对测试结果的代表性或探索成本作可靠判断。[source: I1, I10, I14；ASSUMPTION：这些缺失会限制解释与比较]（confidence: medium）

## Gaps & Open Questions

- [NEEDS CLARIFICATION: 想观察哪类业务负载：新订单、重放、冲突、已确认查询、UNKNOWN核实，还是某种比例？]
- [NEEDS CLARIFICATION: 目标环境是当前宿主程序、应用容器还是其他机器？实际CPU、内存、磁盘、容器配额及数据库有效配置是什么？]
- [NEEDS CLARIFICATION: 目标负载、连接/用户行为、持续时间、数据规模和停止条件分别是什么？]
- [NEEDS CLARIFICATION: 需要的结果是观察趋势与解释瓶颈，还是对明确的吞吐/分位时延/错误率目标作判断？目前没有批准这些数值。]
- [NEEDS CLARIFICATION: 在不改变原业务超时、幂等及结果保护语义的前提下，哪些记录可用于区分客户端受限、连接池等待、数据库锁/I/O与应用计算？当前没有对应实测。]
- [NEEDS CLARIFICATION: 是否仅识别瓶颈，任何优化、安装、运行负载和资源变更仍需后续另行授权？]
- [NEEDS CLARIFICATION: 如后续选用工具，其适用版本、许可与本地运行条件如何核实？本阶段没有选定或安装工具。]

## Sources

### 用户与本次研究记录

- **U1**：本次对话原话、“准确地来说是进行负载测试”、代号“探流（FlowProbe）”，以及用户确认以`flowprobe`研究上述想法。2026-11-13。来源为项目用户陈述，不是线上运行观测。
- **R0**：本次研究执行范围：只读项目源码/文档及三个公开仓库页面；没有负载、benchmark、应用/DB启动、工具安装或优化执行。
- **R1**：本次仓库只读glob/grep与源文件核对；范围为业务代码、scripts、tests、deploy和工作流，关键词为Benchmark、-bench、load/perf/stress、负载/性能及常见负载工具名称。负面检索结论仅限此范围，不证明仓库外没有实验。

### 本地来源（相对项目根目录；非URL，不触发网络抓取）

- **I1**：`.specify/assessments/flowprobe/intake.md`，Idea、Restated、Origin & Context、First-Glance Unknowns；按用户要求从原自动生成目录迁入。
- **I2**：`.specify/memory/constitution.md:65–73`，项目目标与Mock边界。
- **I3**：`tests/us2_concurrency_test.go:77–94,181–235`；`internal/postgres/concurrency_test.go:210–242,302–341`，双请求并发及正确性断言。
- **I4**：`scripts/demo.py:1–7`，单次五步演示客户端定位。
- **I5**：`artifacts/phase6/delivery-review-fixes/acceptance.json:48–56,92–101`，既有修复验收内容及未运行边界。本研究仅读，不重新认证其所有运行环境。
- **I6**：`specs/001-mock-payment/spec.md:95–104`；`plan.md:58–66`，非目标和未设生产性能承诺。
- **I7**：`specs/001-mock-payment/spec.md:108–123,149–155`，业务范围、金额、查询、幂等与非真实资金。
- **I8**：`internal/payment/submit.go:96–137`；`query.go:66–118`，新单、复用与两类查询路径。
- **I9**：`internal/app/business.go:55–73`；`internal/mock/transaction.go:40–53,93–180`；`plan.md:142,164–168`，共享池、独立事务与Mock边界。
- **I10**：`internal/app/app.go:220–303`，连接串白名单、规范化与池配置创建；本次未读取或归档实际连接凭据。
- **I11**：`internal/postgres/orders.go:22–26,119–184`；`results.go:28–36,92–104,153–163,194–230`；`migrations/00001_create_payment_orders.sql:33`、`00002_create_mock_transactions.sql:4`、`00003_create_result_handling_records.sql:39–40`。
- **I12**：`internal/payment/submit.go:26–30`；`internal/postgres/orders.go:15–19`、`results.go:15–18`；`internal/app/lifecycle.go:85–94,210–220`；`plan.md:217–250`。
- **I13**：`deploy/compose.app.yaml:21,48`；`internal/app/testmode.go:52–68,164–178,206–298`；`plan.md:375–405`，模式默认值、同步观测与证据边界。
- **I14**：`deploy/compose.yaml:7–54`；`deploy/compose.app.yaml:8–88`，服务资源声明范围；本次未运行硬件或数据库配置探测。
- **I15**：`plan.md:205–211,268–296`，环境隔离与敏感数据/产物限制。
- **I16**：`plan.md:369–373`，调用与持久效果分证据、2xx不等于付款或追踪完整。

### 公开来源与访问策略

首次按原策略解析`github.com`时，没有可核验的公网地址，故在发出HTTP请求前拒绝抓取。用户随后明确授权通过其配置的代理读取GitHub公开资料，并例外放宽独立目标公网IP核验；该例外仅用于本次研究，不扩展到负载测试目标或其他站点。[source: U1, R0]（confidence: high；cited）

以下三个准确URL均于2026-11-13通过允许的访问路径GET成功（HTTP 200）；TLS证书/主机名校验保留，未跟随重定向、未爬取页面链接、未发送认证信息或cookie；未独立核验代理后的目标公网IP。页面仅作为证据，未执行其安装命令、示例或其他指令；不保存原始HTML或带示例认证头的全文。

- **E1**：https://github.com/grafana/k6 （host: `github.com`；policy: `allowlisted`，附用户明确批准的代理访问例外；页面README的Core features、Example script、Documentation、License）。
- **E2**：https://github.com/tsenart/vegeta （host: `github.com`；policy: `allowlisted`，附同一代理访问例外；页面README的Features、rate/workers/duration、report说明及资源警告）。
- **E3**：https://github.com/giltene/wrk2 （host: `github.com`；policy: `allowlisted`，附同一代理访问例外；页面README的Latency measurement、Coordinated Omission、Benchmarking Tips及自身精度/校准限制）。

这些是本次读取时的可变仓库首页，不是已安装工具版本或固定release验证；未读取其链接页面，未据此判断项目维护活跃度、市场份额或本项目性能。

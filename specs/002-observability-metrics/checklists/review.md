# 需求审查清单：本地压测期间的支付服务指标采集与回看

**Purpose**: 审查规格、计划和契约是否遗漏关键需求、边界条件与验收口径；本清单检验“需求写得是否完整”，不检验实现是否可用。
**Created**: 2026-11-26
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [data-model.md](../data-model.md) · [contracts/metrics.md](../contracts/metrics.md) · [quickstart.md](../quickstart.md)

**Note**: 本清单由 `/speckit.checklist` 生成，用于需求质量审查。
**Review Ownership**: 清单归审查者所有；只有审查者确认需求质量满足时，才把条目标记为 `[x]`。
**Marker Semantics**: `[x]` 表示需求质量已审查通过，不代表实现完成。

## 范围与目标完整性

- [ ] CHK001 规格是否明确写出指标端点“默认关闭、显式配置才开启”，而不只是在 plan 中决定？ [Gap, Spec §FR-001]
- [ ] CHK002 “一次完整运行”“测量轮”“运行证据目录”三者的关系是否在规格与数据模型中一致定义？ [Consistency, Spec §FR-010]
- [ ] CHK003 观察者、基准入口、证据检查脚本三方的职责边界是否明确，是否存在无人负责的步骤？ [Completeness, Spec §FR-020]
- [ ] CHK004 所有固定端口（9091、9090、9187、3000、55432）的冲突检查是否都写入需求，而不只是 PostgreSQL 端口？ [Gap, Spec §FR-019]
- [ ] CHK005 观测栈的范围是否明确列出包含与不包含的组件，避免“监控平台”范围蔓延？ [Clarity, Spec §非目标]

## 术语与一致性

- [ ] CHK006 “指标证据不完整”和“基准不合格”是否在 US4、FR-020、SC-009 中始终区分清楚？ [Consistency, Spec §FR-020]
- [ ] CHK007 “服务端耗时”的起止边界是否在规格、计划、契约中使用同一口径？ [Consistency, Spec §FR-003]
- [ ] CHK008 “整轮 P95”和“滚动窗口 P95”的时间窗语义是否处处一致？ [Clarity, Spec §FR-003]
- [ ] CHK009 “测量轮”和“压测轮次”是否存在混用，是否会让人误以为一次运行只有一轮？ [Ambiguity, Spec §Clarifications]

## 场景覆盖

- [ ] CHK010 是否定义了两次压测运行并发或重叠时的行为，尤其是固定端口和数据目录？ [Coverage, Gap]
- [ ] CHK011 是否定义了归档指标目录失败（磁盘满、权限不足、移动中断）时的处理？ [Coverage, Gap]
- [ ] CHK012 是否定义了监控密码文件在轮次之间被删除、修改或权限变化时的行为？ [Coverage, Gap]
- [ ] CHK013 是否定义了 Grafana provisioning 失败、看板缺失或数据源不可用时的验收表现？ [Coverage, Gap]
- [ ] CHK014 是否定义了应用以指标关闭状态启动、而观测栈仍在运行时的预期行为？ [Coverage, Spec §FR-015]
- [ ] CHK015 是否定义了 exporter 在每轮数据库重建后重连的预期和可观察信号？ [Coverage, Spec §FR-007]

## 边界与失败处理

- [ ] CHK016 `metrics-evidence.json` 判定“完整”的阈值（每次抓取都成功，还是允许少量失败）是否有明确依据？ [Clarity, Data Model]
- [ ] CHK017 当 `up=1` 但某个关键指标序列缺失时，是否仍判定为“不完整”？ [Edge Case, Gap]
- [ ] CHK018 指标目录只读、或已被另一个 Prometheus 实例占用时，期望行为是否定义？ [Edge Case, Gap]
- [ ] CHK019 5 轮运行被 SIGTERM 中断且指标正在写入时，归档和证据标记如何处理？ [Edge Case, Gap]
- [ ] CHK020 “无数据”和“0”的区分是否覆盖所有验收查询，而不只是 5xx 比例？ [Clarity, Contracts §查询契约]

## 非功能需求

- [ ] CHK021 观测栈自身 CPU 开销是否被量化，或明确说明为何接受不量化？ [Non-Functional, Assumption]
- [ ] CHK022 每轮指标数据和数据库卷的保留期限、磁盘上限与清理触发条件是否明确？ [Non-Functional, Gap]
- [ ] CHK023 Grafana 回环匿名只读的安全边界是否作为明确需求记录，并有接受理由？ [Security, Clarification]
- [ ] CHK024 指标名、标签和桶边界变更的兼容规则是否足以防止看板静默失效？ [Maintainability, Contracts §变更规则]
- [ ] CHK025 5 轮协议带来的运行时长和磁盘增长是否在需求或假设中说明？ [Non-Functional, Assumption]

## 验收标准可测性

- [ ] CHK026 SC-001 中“差异只来自时间窗边界”是否有可操作的容差或核对方法？ [Measurability, SC-001]
- [ ] CHK027 每条成功标准是在单轮、5 轮中的某一轮，还是完整运行上验收，是否写明？ [Clarity, SC-001/SC-008]
- [ ] CHK028 `metrics-evidence.json` 要求的“关键序列”是否逐项列出并说明选择依据？ [Completeness, Data Model]
- [ ] CHK029 证据检查和导出脚本的调用方式、输出位置、退出码是否在需求或快速开始中明确？ [Completeness, Quickstart §4/§7]
- [ ] CHK030 “整轮 P95 可与 k6 P95 对照”是否说明允许的测量点差异，避免被误解为数值相等？ [Clarity, SC-001]

## 依赖与假设

- [ ] CHK031 Linux/WSL2 同一来宾、Docker 通过 `host.docker.internal` 访问宿主机的假设是否记录并可在启动时验证？ [Assumption, Research]
- [ ] CHK032 Grafana 13 经典 JSON provisioning 的验证步骤和回退方案是否已分配？ [Dependency, Research §风险]
- [ ] CHK033 镜像与依赖版本固定的规则是否足以复现一次历史运行？ [Dependency, Research §版本固定]
- [ ] CHK034 旧 3 轮实测记录与新 5 轮协议不可直接比较的说明是否足够清楚？ [Assumption, Spec §Assumptions]

## 歧义与冲突

- [ ] CHK035 规格早期“每轮独立目录”的表述是否已完全被“每次运行一个目录”取代，没有残留？ [Conflict, Spec §Clarifications]
- [ ] CHK036 “观测栈缺失时应用仍能启动”与“指标端点开启后失败即启动失败”是否被清楚地区分为两种不同条件？ [Conflict, Spec §FR-001/FR-015]
- [ ] CHK037 5 轮变更属于本期交付还是独立基准变更，是否在规格和计划中给出唯一结论？ [Ambiguity, Spec §FR-018]
- [ ] CHK038 固定端口 55432 与现有基准“动态回环端口”的旧描述是否已明确废弃？ [Conflict, Spec §FR-019]

## Notes

- 只有审查者确认需求质量满足时，才把条目标记为 `[x]`。
- 仍需澄清、修正或人工判断的条目保持未勾选。
- `/speckit.implement` 会读取清单状态，但不会修改标记。
- 可在条目下直接追加发现或评论。
- 条目按顺序编号，便于引用。

## 修订说明（2026-11-26）

规格、计划、数据模型、契约和快速开始已按审查结论修订；以下条目已有直接答案，仍由审查者确认后再勾选：

- CHK001：FR-001 已写明默认关闭和回环地址校验。
- CHK004、CHK013：FR-024/SC-013 已按组件划分端口检查责任，并定义观测栈/回看失败表现。
- CHK010、CHK019：新增排他运行锁；中断按实际完成的测量阶段判定。
- CHK016、CHK017：完整性阈值、`up=1` 但序列缺失已在 FR-021 明确。
- CHK020：契约新增“抓取失败 / 无请求 / 有请求”显示表。
- CHK026、CHK030：SC-001 已给出 ±3ms 或 ±20% 容差和桶边界说明。
- CHK028、CHK029：数据模型逐项列出关键序列；快速开始给出检查/导出路径和退出码。
- CHK031：网络方案已从 `host.docker.internal` 改为 host 网络；该条文字保留为历史，审查时按新方案判断。
- CHK038：`docs/payment-create-baseline.md` 已列入计划交付物，实现时同步为 5 轮和固定 55432。

# Tasks: 本地压测期间的支付服务指标采集与回看

**Input**: Design documents from `/specs/002-observability-metrics/`

**Prerequisites**: plan.md（必需）、spec.md（用户故事与验收）、research.md、data-model.md、contracts/、quickstart.md

**Tests**: 本特性的 plan.md、宪章原则 IV 和指标契约要求自动化验证，因此包含测试任务。实现任务开始前，对应测试必须先失败。

**Organization**: 任务按用户故事分组；每个故事都能独立实现和验证。跨故事的指标运行时放在 Foundational，基准协议与 PostgreSQL 相关改动归入 US3。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可以并行执行（不同文件，无未完成依赖）
- **[Story]**: 所属用户故事，例如 `[US1]`
- 每个任务都带明确文件路径

## Path Conventions

- Go 运行时：`internal/`
- 观测栈：`deploy/`
- 基准与证据脚本：`tests/benchmark/`
- 文档：`docs/`

---

## Phase 1: Setup

**Purpose**: 建立依赖、目录和脚本骨架，不实现行为。

- [ ] T001 在 `go.mod` / `go.sum` 引入 `github.com/prometheus/client_golang v1.25.0`，执行 `go mod tidy` 和 `go build ./...`，确认现有构建不受影响
- [ ] T002 [P] 创建 `deploy/compose.observability.yaml` 与 `deploy/observability/` 目录骨架，包括 `prometheus.yml`、`prometheus.view.yml`、`postgres-exporter-user`、`postgres-exporter-queries.yaml`、`grafana/provisioning/datasources/prometheus.yml`、`grafana/provisioning/dashboards/provider.yml`、`grafana/dashboards/overview.json`
- [ ] T003 [P] 创建 `tests/benchmark/check_metrics_evidence.py` 和 `tests/benchmark/export_metrics.py` 的 argparse 骨架，写入 0/2/3/4 退出码注释；创建 `tests/benchmark/test_metrics_evidence.py`、`tests/benchmark/test_export_metrics.py`，先断言 `--help` 可用、错误参数返回 2
- [ ] T004 [P] 创建 `docs/observability.md` 中文骨架，包含启动、密码文件、验证、归档、回看、导出、清理、限制八节，暂不填入命令

---

## Phase 2: Foundational (指标运行时)

**Purpose**: 指标注册表、地址配置和第二监听；阻塞 US1、US2、US4 的应用侧实现。

**⚠️ CRITICAL**: 本阶段完成后 US1 和 US2 才能开始。

- [ ] T005 实现 `internal/metrics/registry.go` 的 `NewRegistry()`：使用 `prometheus.NewRegistry()` 注册 Go collector 和 process collector；在 `internal/metrics/registry_test.go` 断言 `go_goroutines`、`go_memstats_heap_inuse_bytes`、`process_cpu_seconds_total`、`process_resident_memory_bytes` 存在
- [ ] T006 在 `internal/app/app.go` 增加 `PAYMENT_HUB_METRICS_ADDR` 解析：未设置或空字符串关闭；非空必须是字面回环 IP:port，拒绝 `0.0.0.0`、`::`、空 host 和非回环主机名；在 `internal/app/metrics_config_test.go` 覆盖合法、空值、非法和监听失败前置条件
- [ ] T007 在 `internal/app/lifecycle.go` 集成第二个监听：业务监听成功后再建立指标监听，使用 `promhttp.HandlerFor(registry, ...)` 提供 `/metrics`；任一监听失败关闭已建立资源；两个 server 共享同一个 shutdown deadline，先停指标监听再停业务 server；在 `internal/app/metrics_lifecycle_test.go` 覆盖关闭、开启、监听失败和优雅关闭

**Checkpoint**: 应用可以在显式配置后于 `127.0.0.1:9091` 暴露 Go/process 指标，默认行为不变。

---

## Phase 3: User Story 1 - 压测时能看清服务端接口表现 (Priority: P1) 🎯 MVP

**Goal**: 三个业务接口的请求计数、错误比例和耗时分布可按方法、路由模板、状态码查询；`/readyz` 不污染业务指标；指标端点默认关闭。

**Independent Test**: 只启动应用和 Prometheus，发送正常、401、5xx 和无流量请求，抓取 `/metrics`，检查计数、状态码标签、桶边界和 `unmatched` 行为，不依赖数据库和连接池。

### Tests for User Story 1

- [ ] T008 [P] [US1] 在 `internal/metrics/http_contract_test.go` 写失败测试：`method`/`route`/`status` 标签、`unmatched` 固定值、`/readyz` 跳过、401 计入、无订单号/支付 ID/请求 ID、20 个直方图桶
- [ ] T009 [P] [US1] 在 `internal/metrics/http_test.go` 写失败测试：无 5xx 时 `status=~"5.."` 序列不存在；出现 5xx 后计数增加；`go_gc_duration_seconds_count` 等运行时序列不被 HTTP 指标覆盖

### Implementation for User Story 1

- [ ] T010 [US1] 实现 `internal/metrics/http.go`：全局中间件按 `c.FullPath()` 或 `unmatched`、`c.Writer.Status()` 精确状态码、方法记录计数器；耗时从进入路由组到 `c.Next()` 返回；`/readyz` 跳过；注册 20 个桶（0.001 到 5 秒）
- [ ] T011 [US1] 实现 `RegisterHTTP`/`Handler` 并在 `internal/metrics/registry.go` 或 `http.go` 暴露注册入口；单元测试断言指标名、标签和类型与 `contracts/metrics.md` 一致
- [ ] T012 [US1] 在 `internal/httpapi/router.go` 为 `NewRouter` 和 `NewRouterWithPayments` 增加全局中间件选项，保持 `/readyz` 与三个业务路由注册顺序不变；更新 `internal/httpapi/router_payments_test.go`
- [ ] T013 [US1] 在 `internal/app/lifecycle.go` 把 HTTP 指标注册到 T007 的注册表，确保指标监听与业务监听共享同一个 registry；新增 `internal/app/metrics_http_integration_test.go`，实际抓取 `127.0.0.1:9091/metrics`
- [ ] T014 [US1] 新增 `internal/httpapi/router_metrics_test.go`：断言引入中间件后支付响应体、状态码和鉴权行为不变；未配置 `PAYMENT_HUB_METRICS_ADDR` 时不存在指标监听

**Checkpoint**: US1 可独立演示；HTTP 指标可由 Prometheus 抓取，业务契约不变。

---

## Phase 4: User Story 2 - 压测时能看清应用资源和连接池 (Priority: P1)

**Goal**: 能看到 goroutine、堆、GC、CPU、常驻内存，以及连接池上限、使用中、空闲、获取次数、无空闲等待和平均等待。

**Independent Test**: 用 fake pool 快照和真实 `pgxpool` 小池分别验证；不需要 PostgreSQL exporter。

### Tests for User Story 2

- [ ] T015 [P] [US2] 在 `internal/metrics/pool_test.go` 定义 `PoolStats` 窄接口并写失败测试：12 个连接池指标的数值映射、类型和标签
- [ ] T016 [P] [US2] 在 `internal/metrics/pool_metrics_test.go` 断言指标名与 `contracts/metrics.md` 完全一致，Gauge 不使用 `_total`，计数器使用 `_total`
- [ ] T017 [US2] 在 `internal/metrics/pool_saturation_test.go` 用 max=2 的测试池并发获取 3 个连接，断言 `payment_hub_db_pool_empty_acquires_total` 增加且平均等待可计算

### Implementation for User Story 2

- [ ] T018 [US2] 实现 `internal/metrics/pool.go`：`prometheus.Collector` 从 `PoolStats` 快照读取上限、总数、空闲、使用中、建立中、获取、累计等待、无空闲等待、取消、新建和销毁；单个业务池，无池标签
- [ ] T019 [US2] 在 `internal/app/app.go` 通过 `BusinessPool()` 把业务池接入采集器，确保不新建第二个池；新增 `internal/app/metrics_pool_test.go`

**Checkpoint**: US2 可独立演示；停压后资源和池指标可观察回落。

---

## Phase 5: User Story 3 - 压测时能看清 PostgreSQL 在做什么 (Priority: P1)

**Goal**: 通过固定端口和只读 exporter 持续采集连接、事务、锁等待、缓存、临时文件和检查点；数据库停止与 exporter 停止可区分。基准入口同步固定为 5 轮、55432 端口并持有运行锁。

**Independent Test**: 只启动 PostgreSQL、exporter 和 Prometheus，跑一轮基准后查询；停数据库观察 `up=1,pg_up=0`，停 exporter 观察 `up=0`；第二个运行在锁被持有时立即失败。

### Tests for User Story 3

- [ ] T020 [P] [US3] 新增 `tests/benchmark/test_observability_compose.py`：断言 `deploy/compose.observability.yaml` 使用 `network_mode: host`、无 `ports`、无 `host.docker.internal`，Prometheus/exporter/Grafana 分别绑 `127.0.0.1:9090/9187/3000`
- [ ] T021 [P] [US3] 新增 `tests/benchmark/test_prometheus_config.py`：断言 `prometheus.yml` 抓取 `127.0.0.1:9091` 和 `127.0.0.1:9187`、间隔 5 秒、`scrape_timeout` 4 秒；`prometheus.view.yml` 无抓取目标且启用长保留
- [ ] T022 [P] [US3] 新增 `tests/benchmark/test_exporter_queries.py`：解析 `postgres-exporter-queries.yaml`，断言存在 `waiting_locks` 和 `granted_locks` 列，且不产生 `usename`/`application_name` 标签
- [ ] T023 [US3] 新增 `tests/benchmark/test_runner_logic.py` 用例：5 轮常量、55432/9091 预检、`<artifacts root>/.payment-hub-load.lock` 排他锁、锁竞争立即失败、中断阶段标记
- [ ] T024 [US3] 新增 `tests/benchmark/test_runner_secret_hygiene.py`：用假密码运行角色创建路径，断言密码不出现在 `commands.log`、`summary.json`、进程参数和异常文本中

### Implementation for User Story 3

- [ ] T025 [US3] 修改 `tests/benchmark/run_payment_create.py`：轮数固定为 5；启动前检查 `55432` 和 `9091`；在 `<artifacts root>/.payment-hub-load.lock` 上持有排他 `flock` 到运行结束；冲突时输出锁路径/PID 并立即失败
- [ ] T026 [US3] 修改 `tests/benchmark/run_payment_create.py`：preflight 读取一次 `PAYMENT_HUB_MONITOR_PASSWORD_FILE` 并校验 `0600`；5 轮复用同一密码；通过 stdin 执行 `CREATE ROLE postgres_exporter ...`，不把密码放进 argv；启动应用时注入 `PAYMENT_HUB_METRICS_ADDR=127.0.0.1:9091`；确保 `commands.log` 和 `summary.json` 不出现密码
- [ ] T027 [US3] 更新 `tests/benchmark/compose.benchmark.yaml` 和 `deploy/compose.yaml`：PostgreSQL 固定发布到 `127.0.0.1:55432`，应用指标地址按 T026 注入；同步更新 `tests/benchmark/test_runner_logic.py` 中的端口和 env 断言
- [ ] T028 [P] [US3] 更新 `docs/payment-create-baseline.md`：改为 5 个测量轮、固定 `55432`，替换 `--rounds 3` 示例，并在文首标注旧 3 轮记录为历史协议、不与新协议直接比较
- [ ] T029 [US3] 实现 `deploy/compose.observability.yaml`：固定 `prom/prometheus:v3.16.0`、`grafana/grafana:13.2.7`、`prometheuscommunity/postgres-exporter:v0.21.1`；host 网络；Prometheus/exporter 以 `${HOST_UID}:${HOST_GID}` 运行；密码文件只读挂载到 `/run/payment-hub-monitor-password`；exporter 使用 `--datasource.uri=127.0.0.1:55432/postgres?sslmode=disable`
- [ ] T030 [US3] 实现 `deploy/observability/prometheus.yml` 和 `prometheus.view.yml`：压测配置 5 秒抓取两个目标；回看配置空抓取、`--storage.tsdb.retention.time=100y`
- [ ] T031 [US3] 实现 `postgres-exporter-user` 和 `postgres-exporter-queries.yaml`，并在 Compose 中配置 `--collection-timeout=3s`、`--extend.query-path`、关闭 `settings`/`stat_activity`/`stat_user_tables`/`statio_*`/`locks`/`replication*`，保留 `stat_database` 和 `stat_bgwriter`
- [ ] T032 [US3] 新增 `tests/benchmark/verify_observability.py`：启动观测栈后断言 `pg_up` 出现、`pg_benchmark_waiting_locks` 出现、`pg_stat_database_xact_commit` 出现，且 `/metrics` 不含 `usename`/`application_name`
- [ ] T033 [US3] 新增 `tests/benchmark/lock_wait_demo.py`：用 `psql` 持有行/表锁，触发支付更新，断言 `pg_benchmark_waiting_locks > 0`，并记录可复现命令
- [ ] T034 [US3] 新增 `tests/benchmark/reconnect_demo.py`：重启基准数据库，断言 `up{job="postgres"}=1` 保持、`pg_up` 自动回到 1、exporter 不重启

**Checkpoint**: US3 可独立演示数据库侧指标、`up`/`pg_up` 四态、固定端口、5 轮和运行锁。

---

## Phase 6: User Story 4 - 压测后用 Grafana 回看和分析 (Priority: P1)

**Goal**: 每次运行的指标归档到证据目录，按 5 个测量阶段生成证据；用 Grafana 预置看板回看；关键查询可导出。

**Independent Test**: 用一次运行的数据副本启动 Prometheus 和 Grafana，打开预置看板；断网/停应用时仍能看该次运行窗口；用另一次运行的数据确认不混淆。

### Tests for User Story 4

- [ ] T035 [P] [US4] 新增 `tests/benchmark/test_metrics_evidence.py`：用 fake Prometheus HTTP 服务器覆盖完整、缺阶段、`up=0`、`pg_up=0`、抓取次数不足、Prometheus 不可达和原子写入
- [ ] T036 [P] [US4] 新增 `tests/benchmark/test_export_metrics.py`：覆盖每个契约查询的 CSV、JSON 汇总、时间窗和 PromQL 记录、退出码 0/2/4
- [ ] T037 [P] [US4] 新增 `tests/benchmark/test_metrics_store.py`：归档复制到 `metrics.tmp/`、校验 `wal/`、rename 为 `metrics/`；回看复制到 `VIEW_DIR` 后归档目录哈希不变
- [ ] T038 [P] [US4] 新增 `tests/benchmark/test_dashboard_json.py`：解析 `overview.json`，断言经典 JSON 结构、固定 UID、四类指标面板和契约查询名存在

### Implementation for User Story 4

- [ ] T039 [US4] 实现 `tests/benchmark/check_metrics_evidence.py`：从 `summary.json` 读取 5 个 `load200` 阶段的 `k6_started_epoch`/`k6_finished_epoch`；按阶段查询 `up`、`pg_up`、`count_over_time` 和关键序列；`expected = max(0, floor(窗口秒数 / 5) - 1)`；写入 `payment-hub.metrics-evidence.v2`；`status` 只有 `complete`/`incomplete`，`run_id` 必须等于 `summary.json`；退出码 0 完整、3 不完整、2 参数/IO、4 Prometheus 不可达；先写临时文件再 `os.replace`
- [ ] T040 [US4] 实现 `tests/benchmark/export_metrics.py`：按 `contracts/metrics.md` 查询，写入 `$RUN_DIR/metrics-export/`，每个查询一份 CSV 加 `queries.json`；退出码 0/2/4
- [ ] T041 [US4] 实现 `tests/benchmark/metrics_store.py` 的 `archive` 和 `view` 子命令：归档复制到 `metrics.tmp/`、校验 `wal/`、rename 为 `metrics/`；回看复制到 `VIEW_DIR`，归档保持不变
- [ ] T042 [US4] 实现 `deploy/observability/grafana/provisioning/datasources/prometheus.yml`、`provisioning/dashboards/provider.yml` 和 `dashboards/overview.json`：数据源 `editable: false`，看板 `allowUiUpdates: false`，匿名 Viewer，关闭 basic auth/登录表单/注册
- [ ] T043 [US4] 新增 `tests/benchmark/verify_grafana.py`：用 `grafana/grafana:13.2.7` 启动，断言数据源和看板 UID 自动加载、3000 只绑回环；失败时记录回退到 12.4.16 或 V2 的决策
- [ ] T044 [US4] 更新 `docs/observability.md`：写入手工起停、归档、回看、导出和清理命令；清理必须按 `summary.json.overall.volumes` 删除基准数据库卷，不能把 `down -v` 当基准卷清理

**Checkpoint**: US4 可独立用已归档数据回看；指标证据不影响基准资格。

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T045 [P] 新增 `internal/metrics/cardinality_test.go`：gather 应用注册表，断言一次运行少于 300 条序列，且不存在 `usename`、`application_name`、订单号、支付 ID、请求 ID 标签
- [ ] T046 [P] 更新 `specs/002-observability-metrics/quickstart.md`：把命令替换为 T039–T042 的实际参数，补充 host 网络、UID/GID、归档副本和清理卷的验证步骤
- [ ] T047 运行 `go test ./...` 和 `python3 -m unittest discover -s tests/benchmark -p 'test_*.py'`，记录实际命令、退出码和失败修复；不得用旧结果代替
- [ ] T048 在 Linux/WSL2 执行完整 quickstart：启动观测栈、跑 5 轮、生成证据、归档、回看、导出、清理；记录实际结果、未验证项和限制
- [ ] T049 运行 `git diff --check`，核对暂存文件只包含本期范围；提交与推送需单独授权

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: 无依赖，立即开始
- **Foundational (Phase 2)**: 依赖 T001；阻塞 US1、US2、US4 的应用侧实现
- **US1 (Phase 3)**: 依赖 Phase 2；不依赖 US2/US3
- **US2 (Phase 4)**: 依赖 Phase 2；不依赖 US1/US3
- **US3 (Phase 5)**: 依赖 Phase 2 和 T001；基准协议与 PostgreSQL 指标一起交付
- **US4 (Phase 6)**: 证据/导出/存储可在 US3 的 summary 结构确定后开发；真实回看依赖 US1–US3 的数据
- **Polish (Phase 7)**: 依赖所有已选故事

### Within Each Story

- 测试先写并确认失败，再实现
- 采集器/中间件先于注册和接线
- 注册表接入先于端到端抓取测试
- 组件实现后再跑该故事的独立验收

### Parallel Opportunities

- T002、T003、T004 可并行
- T005、T006 可并行；T007 依赖两者
- T008、T009 可并行；T010 → T011 → T012 → T013 顺序
- T015、T016 可并行；T017 依赖 T015；T018 → T019 顺序
- T020、T021、T022 可并行；T023/T024 可在 T025/T026 后补测；T025 → T026 → T027 顺序
- T028 可与 T025–T027 并行（不同文件）
- T029–T031 在 T020–T022 之后实现；T032–T034 依赖 T029–T031
- T035、T036、T037、T038 可并行；T039–T043 按依赖顺序
- T045、T046 可并行

---

## Parallel Example: User Story 1

```text
T008 [P] [US1] 写 HTTP 指标契约测试
T009 [P] [US1] 写 5xx 序列存在性测试
```

## Parallel Example: User Story 3

```text
T020 [P] [US3] Compose host 网络静态测试
T021 [P] [US3] Prometheus 抓取配置测试
T022 [P] [US3] 自定义查询 YAML 测试
```

## Parallel Example: User Story 4

```text
T035 [P] [US4] 证据检查测试
T036 [P] [US4] 导出测试
T037 [P] [US4] 归档/回看复制测试
T038 [P] [US4] 看板 JSON 测试
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. 完成 Phase 1 和 Phase 2
2. 完成 US1（T008–T014）
3. 用应用 + Prometheus 独立验证三个接口的计数、错误和 P95
4. 此时已有可演示的最小指标闭环

### Incremental Delivery

1. Setup + Foundational → 指标端点可抓 Go/process
2. US1 → HTTP 指标（MVP）
3. US2 → 资源与连接池
4. US3 → 基准协议 + PostgreSQL 指标与四态
5. US4 → 证据、看板、导出和归档回看
6. Polish → 基数、文档、全量验证和 quickstart 实测

### Notes

- `[P]` 任务必须确认不同文件且无未完成依赖
- 测试必须先失败，再实现
- 每个故事结束停在 Checkpoint，独立验证后再进入下一故事
- 不自动提交或推送；提交需单独授权
- Grafana 13 经典 JSON、exporter 自定义查询和完整压测链路属于实现时验证项，失败时按 `research.md` 的回退方案处理

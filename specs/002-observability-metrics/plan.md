# Implementation Plan: 本地压测期间的支付服务指标采集与回看

**Branch**: `002-observability-metrics` | **Date**: 2026-11-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-observability-metrics/spec.md`

## Summary

为本地压测增加一套按需启动的指标采集与回看能力：Payment Hub 在显式配置时于回环地址暴露 `/metrics`，包含 Gin 请求指标、Go 运行时/进程指标和 `pgxpool` 连接池指标；PostgreSQL 统计由独立 exporter 提供。仓库提供 Docker Compose 观测栈（Prometheus、Grafana、postgres_exporter），压测前手动启动、压测后归档指标数据并用单一看板回看。基准入口同步改为 5 个测量轮，PostgreSQL 使用固定回环端口 55432，监控账号密码来自本地 `0600` 文件。不改变支付业务契约，不建设常驻监控平台。

## Technical Context

**Language/Version**: Go 1.27.4（沿用 `go.mod`）

**Primary Dependencies**: Gin v1.12.0、pgx v5.11.0、`github.com/prometheus/client_golang v1.25.0`

**Storage**: Prometheus 本地 TSDB，每次运行一份，归档到该次运行的证据目录；PostgreSQL 统计由 `prometheuscommunity/postgres-exporter:v0.21.1` 提供

**Testing**: Go 单元测试（注册表、中间件、连接池采集器、配置）、Python 单元测试（基准入口、证据检查、导出）、手动 Compose 验收（host 网络、exporter 自定义查询、Grafana 经典 JSON、归档副本回看）

**Target Platform**: Linux / WSL2 同一来宾，本机回环；Docker Compose host 网络

**Project Type**: 单个 Go Web 服务 + 本地运维脚本

**Performance Goals**: 压测期间抓取间隔 5 秒；不单独测量观测栈自身开销；对照运行保持相同观测栈配置；沿用 200 RPS × 120 秒 × 5 轮

**Constraints**: 指标端点按需开启且仅回环；PostgreSQL 固定端口 55432；监控密码使用本地 `0600` 文件，不进入仓库、环境变量、命令行或日志；观测栈使用 host 网络并以宿主 UID/GID 运行；观测栈用完即停；不采 cAdvisor 和 `pg_stat_statements`

**Scale/Scope**: 单服务、单操作者、约 200 RPS、一次运行少于 300 条时间序列；一次运行 5 个测量轮

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| 原则 | 检查结论 |
|---|---|
| I. 业务契约与小步交付 | 指标能力独立交付，规格、计划、文档以中文为主；不混入支付业务改动 |
| II. 金额与状态正确性 | 不修改金额、币种、订单状态或结果语义 |
| III. 不可靠交互与幂等 | 不修改支付提交、通知或幂等逻辑；指标仅旁路记录 |
| IV. 风险驱动的验证与可追溯 | 每项指标契约有单元测试；压测证据包含指标数据与 `metrics-evidence.json` |
| V. 简单设计与人类负责 | 不引入常驻平台、告警和长期存储；观测栈按需启动，默认行为不变 |

**结论**：通过，无宪法违反项。

## Key Technical Decisions

- **指标端点按需开启**：仅当设置非空 `PAYMENT_HUB_METRICS_ADDR` 时启动第二个监听；地址必须是字面回环 IP:port，拒绝 `0.0.0.0`、`::`、空 host 和非回环主机名；未设置或空值不启动监听；开启后监听失败视为启动失败。
- **第二监听与现有生命周期集成**：在现有启动预算内同时建立业务监听和指标监听，任一失败按配置语义关闭已建立资源；两个 server 共享同一个 shutdown deadline，指标监听先停止并只使用最多 500ms 或四分之一预算的受限子窗口，随后业务 server 使用剩余预算；metrics 清理失败不跳过 gate drain、testMode.Seal 和数据库关闭；连接池通过窄接口传给采集器。
- **独立注册表**：使用 `prometheus.NewRegistry()`，注册 Go collector、process collector、连接池采集器和 HTTP 指标，不使用全局默认注册表，避免测试污染。
- **Gin 指标中间件**：全局注册在路由之前，按方法、`c.FullPath()` 路由模板、精确状态码和耗时记录；`/readyz` 跳过；未匹配路由归入固定值 `unmatched`；计时到 handler 链返回为止，包含响应写入但不含 socket 刷新。
- **连接池采集器**：实现 `prometheus.Collector`，从 `pgxpool.Stat()` 读取上限、总数、空闲、使用中、建立中、获取次数、累计等待、无空闲等待、取消和连接销毁计数；单个业务池，不加池标签。
- **观测栈 host 网络**：`deploy/compose.observability.yaml` 使用 `network_mode: host`，Prometheus 绑 `127.0.0.1:9090`、exporter 绑 `127.0.0.1:9187`、Grafana 绑 `127.0.0.1:3000`；抓取 `127.0.0.1:9091` 和 `127.0.0.1:9187`。要求 Linux/WSL2 同一来宾，不支持 Docker Desktop。
- **固定端口与运行锁**：应用指标 9091、Prometheus 9090、Grafana 3000、exporter 9187、基准 PostgreSQL 55432，全部绑定回环。基准入口检查 55432 和 9091 并持有 `/tmp/payment-hub-load.lock`（固定路径，与 `TMPDIR`/`TEMP`/`TMP` 无关）排他锁；锁是主机级、与 `--artifacts-dir` 无关，因为 `55432`/`9091` 是主机级固定资源，不同证据目录的运行同样不能并发；观测栈 Compose 负责 9090/9187；3000 只在回看阶段检查。
- **镜像与依赖固定**：`prom/prometheus:v3.16.0`、`grafana/grafana:13.2.7`、`prometheuscommunity/postgres-exporter:v0.21.1`、`client_golang v1.25.0`；首次实现记录 linux/amd64 digest。
- **exporter 凭据与身份**：`--datasource.uri=127.0.0.1:55432/postgres?sslmode=disable`、`--datasource.user-file`、`--datasource.pass-file`，连接 `postgres` 库读取整个实例统计；Prometheus 和 exporter 以宿主 UID/GID 运行，密码文件只读挂载到 `/run/payment-hub-monitor-password`；权限由入口脚本检查。
- **监控角色**：基准入口每轮用本地密码文件中的密码创建只读角色 `postgres_exporter`，授予 `pg_monitor` 和连接权限；SQL 通过 stdin 传递，密码不进入 argv、日志或摘要。
- **exporter collector 配置**：显式关闭 `settings`、`stat_activity`、`stat_user_tables`、`statio_*`、`locks`、`replication*` 等 collector；保留 `stat_database` 和 `stat_bgwriter`；锁等待由 `deploy/observability/postgres-exporter-queries.yaml` 的自定义查询提供 `pg_benchmark_waiting_locks`，不带用户标签。
- **Grafana 访问**：host 网络绑回环，启用匿名 Viewer，关闭 basic auth、登录表单和注册；数据源和看板全部由文件预置，`editable: false`、`allowUiUpdates: false`。
- **归档与回看**：压测期间 Prometheus 写入临时数据目录；运行结束后归档到证据目录 `metrics/`；回看时把归档复制到临时副本，用空的抓取配置和 `--storage.tsdb.retention.time=100y` 指向副本启动 Prometheus，归档保持不变。
- **证据检查**：压测后脚本按 `summary.json` 的 5 个测量阶段窗口查询 `up`、`pg_up`、抓取次数和关键序列，写入 `metrics-evidence.json`；每次 `up=1`、`pg_up=1`、抓取次数不少于期望值且序列齐全才算该阶段完整；指标中断不改变基准资格，但标记该运行不用于指标验收。
- **导出**：`tests/benchmark/export_metrics.py` 把契约查询写入 `$RUN_DIR/metrics-export/`，每个查询一份 CSV 加一份 JSON 汇总，记录时间窗和 PromQL。
- **运行标识**：沿用现有 `summary.json` 的 12 位 `run_id`；证据目录名为 `<UTC 时间戳>-<run_id>`；轮次标识为 `<run_id>rN`；`metrics-evidence.json` 记录 `run_id` 和 `run_dir`。

## Project Structure

### Documentation (this feature)

```text
specs/002-observability-metrics/
├── plan.md              # 本文件
├── research.md          # 版本、exporter 配置、Prometheus 归档回看
├── data-model.md        # 指标、标签、运行标识、证据标记
├── quickstart.md        # 启动、压测、归档、回看、清理
├── contracts/
│   └── metrics.md       # 指标名、标签、PromQL 契约
└── tasks.md             # 由 /speckit.tasks 生成
```

### Source Code (repository root)

```text
internal/
├── metrics/                     # 新增：注册表、HTTP 指标、连接池采集器、handler
│   ├── registry.go
│   ├── http.go
│   ├── pool.go
│   └── *_test.go
├── app/
│   ├── app.go                   # 指标地址配置、连接池获取
│   └── lifecycle.go             # 第二监听与 server 生命周期
└── httpapi/
    └── router.go                # 全局中间件选项，保持支付路由不变

deploy/
├── compose.observability.yaml   # Prometheus + Grafana + postgres_exporter，host 网络
└── observability/
    ├── prometheus.yml           # 压测期间抓取配置，5 秒，抓 127.0.0.1
    ├── prometheus.view.yml      # 回看配置，无抓取目标
    ├── postgres-exporter-user   # 只读角色名，非秘密
    ├── postgres-exporter-queries.yaml  # 等待锁自定义查询
    └── grafana/
        ├── provisioning/datasources/prometheus.yml
        ├── provisioning/dashboards/provider.yml
        └── dashboards/overview.json

tests/benchmark/
├── run_payment_create.py        # 5 轮、固定 55432、监控角色、指标地址
├── check_metrics_evidence.py    # 查询各测量阶段并写 metrics-evidence.json
├── export_metrics.py            # 导出关键查询为 CSV/JSON
└── test_*.py

docs/
├── observability.md             # 操作手册与限制
└── payment-create-baseline.md   # 同步为 5 轮、固定 55432，并标注旧 3 轮为历史
```

**Structure Decision**: 沿用现有单项目分层。指标能力放入新的 `internal/metrics` 包；`internal/httpapi` 只增加一个全局中间件选项，不感知 Prometheus；`internal/app` 负责配置和第二个监听的生命周期；观测栈配置集中在 `deploy/observability/`；基准与证据脚本集中在 `tests/benchmark/`。

## Complexity Tracking

| 项 | 说明 |
|---|---|
| 新增第二监听 | 指标端点必须与业务端口分离（FR-001）；纳入现有启动/关闭预算，不另建进程 |
| 新增本地 Compose 观测栈 | 需求明确要求 Prometheus、Grafana 和 exporter；按需启动，不作为常驻服务 |
| 基准入口改动 | 5 个测量轮、固定 PostgreSQL 端口和监控角色来自本期规格，属于同一交付范围 |
| host 网络与宿主 UID | 只有 host 网络能让容器访问绑定在宿主 loopback 的应用和数据库；代价是仅支持 Linux/WSL2 同一来宾，且需以宿主 UID 运行 Prometheus/exporter |

无宪法违反项，无需额外豁免。

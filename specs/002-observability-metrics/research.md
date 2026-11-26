# 研究记录

**Feature**: `002-observability-metrics` | **Date**: 2026-11-26

本文件记录 plan 阶段确认的版本、机制、取舍和未验证项。实现时以本文件为基础，但最终以实际运行证据为准。

## 版本固定

| 组件 | 版本 | 依据 |
|---|---|---|
| Go | 1.27.4 | 仓库 `go.mod` |
| Gin | v1.12.0 | 仓库 `go.mod` |
| pgx | v5.11.0 | 仓库 `go.mod` |
| `prometheus/client_golang` | v1.25.0 | Go 模块代理返回的当前稳定版本 |
| Prometheus | `prom/prometheus:v3.16.0` | Docker Hub 固定标签（linux/amd64 摘要见下） |
| Grafana | `grafana/grafana:13.2.7` | 本项目固定版本；13.3.2 为更新补丁版，升级需单独验证 |
| PostgreSQL exporter | `prometheuscommunity/postgres-exporter:v0.21.1` | Docker Hub 固定标签（linux/amd64 摘要见下） |
| PostgreSQL | 16.15 | 仓库 `deploy/compose.yaml` 已固定镜像摘要 |

选择固定版本而不是浮动标签，保证压测结果和看板可复现。2026-11-26 查询到的 linux/amd64 摘要：

- `prom/prometheus:v3.16.0` → `sha256:e04987641b57704f408e31f6373ba4138ecf27cb4a165d24aabb6c38fddd0f06`
- `grafana/grafana:13.2.7` → `sha256:9fd7619deb8923fc422f2183c3f82fc8dc2f668fd2b54b087336f597cfd92b0d`
- `prometheuscommunity/postgres-exporter:v0.21.1` → `sha256:3b8e05a84282cd1199ad2fb6c66f362738bf4c68c1547d8f4226a34a7dd6d8d7`

Grafana 13.3.2 已是更新的补丁版；本期仍固定 13.2.7，升级需单独验证看板可加载。

## 已核实的实现约束

以下事实在 2026-11-26 直接检查镜像配置、exporter 源码和现有基准入口得到，不是推测：

1. **镜像运行用户**：`prom/prometheus:v3.16.0` 和 `prometheuscommunity/postgres-exporter:v0.21.1` 的 `User` 都是 `nobody`；`grafana/grafana:13.2.7` 是 `472`。因此宿主用户的 `0600` 文件、`0700` 目录，默认容器身份读不到；必须让 Prometheus 和 exporter 以宿主 UID/GID 运行。
2. **exporter 默认 collector**：v0.21.1 默认启用 `settings`、`stat_activity`、`locks`、`replication`、`replication_slots`、`stat_user_tables`、`statio_*` 等；`settings` 会按 `pg_settings.name` 产生大量序列，`pg_stat_activity_count` 带 `datname`、`state`、`usename`、`application_name`、`backend_type`、`wait_event_type`、`wait_event` 七个标签。必须显式关闭后再评估基数。
3. **`pg_locks_count` 不能区分等待**：该指标只有 `datname` 和 `mode` 标签，没有 `granted`。等待锁必须用自定义查询。
4. **`pg_up` 语义**：`up{job="postgres"}` 表示 Prometheus 能否抓到 exporter；`pg_up` 表示 exporter 能否连数据库。数据库停止时 `up=1`、`pg_up=0`。
5. **现有运行标识**：`run_payment_create.py` 的 `summary.json.run_id` 是 12 位十六进制；轮次是 `<run_id>rN`；证据目录名才是 `<时间戳>-<run_id>`。每个阶段的记录已有 `k6_started_epoch` / `k6_finished_epoch`。
6. **PromQL 空向量**：没有请求时 `sum(rate(...))` 为空；5xx 比例的分母存在、分子为空时结果仍为空。要显示 0 必须显式 `or vector(0)`，并区分“无数据”和“0”。

## 观测栈网络

容器经 `host.docker.internal` 到达的是 Docker 网桥网关地址，不是宿主 loopback；应用指标和基准 PostgreSQL 都绑在 `127.0.0.1`，桥接网络下的容器无法访问。因此观测栈使用 `network_mode: host`：

- Prometheus：`--web.listen-address=127.0.0.1:9090`，抓取 `127.0.0.1:9091` 和 `127.0.0.1:9187`。
- exporter：`--web.listen-address=127.0.0.1:9187`，数据源 `127.0.0.1:55432/postgres?sslmode=disable`。
- Grafana：`GF_SERVER_HTTP_ADDR=127.0.0.1`，`GF_SERVER_HTTP_PORT=3000`。
- 删除 `host.docker.internal`、`host-gateway` 和端口映射。

代价是只支持 Linux/WSL2 同一来宾的 Docker Engine；Docker Desktop 的 host 网络行为不同，不作为支持目标。另一个可选方案是让指标和数据库绑定网桥地址，但那会放弃“仅回环”，因此不采用。

## PostgreSQL exporter

`v0.21.1` 支持以下与凭据相关的参数：

- `--datasource.uri`：只包含主机、端口、数据库和查询参数，不含用户名密码。
- `--datasource.user-file`：从文件读取用户名。
- `--datasource.pass-file`：从文件读取密码，密码不出现在命令行或环境变量。

决策：

- 连接 `postgres` 库即可读取整个实例的统计，不需要知道基准当前使用的应用数据库名。
- 用户名固定为 `postgres_exporter`，用户名文件可以随仓库提供（不是秘密）；密码文件由观察者本地创建，权限 `0600`，以只读方式挂载到 `/run/payment-hub-monitor-password`。
- 只读角色授予 `pg_monitor` 和连接权限；不授予写权限。
- exporter 以宿主 UID/GID 运行；权限检查由基准入口完成，exporter 不自行 stat。
- 显式关闭 `settings`、`stat_activity`、`stat_activity_autovacuum`、`stat_user_tables`、`statio_user_tables`、`statio_user_indexes`、`stat_progress_vacuum`、`locks`、`database`、`replication`、`replication_slots`、`database_wraparound`、`long_running_transactions`、`process_idle`；保留 `stat_database` 和 `stat_bgwriter`。
- 锁等待用 `--extend.query-path` 指向 `deploy/observability/postgres-exporter-queries.yaml`，输出 `pg_benchmark_waiting_locks` 和 `pg_benchmark_granted_locks`，不带用户标签。
- `--collection-timeout=3s`，小于 5 秒抓取间隔。
- 不启用 `pg_stat_statements` collector，避免 SQL 文本和额外扩展。

自定义查询的 `--extend.query-path` 在 v0.21.1 已标记 deprecated，但仍是该版本支持的方式；实现时必须实际拉取镜像验证输出名称。若不可用，回退为接受 `stat_activity` 并重新评估基数预算。

## Prometheus 归档与回看

Prometheus 打开 TSDB 需要写锁，启动和运行时会写 WAL、可能压缩块；默认 `--storage.tsdb.retention.time=15d`，打开旧归档还会在启动时删除过期块。因此归档目录不能当作只读证据直接挂给 Prometheus。

实现方式：

- 压测期间写入观察者指定的临时目录。
- 压测后停止 Prometheus，复制到运行证据目录的 `metrics.tmp/`，校验后 rename 为 `metrics/`。
- 回看时把归档复制到另一个临时副本，用同一镜像、空 `scrape_configs` 和 `--storage.tsdb.retention.time=100y` 指向副本；归档目录保持不变。
- 同一副本同时只能有一个 Prometheus 实例。
- 副本需要额外磁盘，大小约为归档的 1 倍；清理时一并删除。

未采用远程写入或对象存储：当前只需要单轮本地回看，远程方案增加依赖且不改变分析能力。

## Grafana provisioning

- 数据源放在 `provisioning/datasources/`，看板 provider 放在 `provisioning/dashboards/`。
- 看板使用经典 JSON，固定 UID，文件放在 `dashboards/`。
- 绑定 `127.0.0.1:3000`；认证只保留匿名 Viewer：`GF_AUTH_ANONYMOUS_ENABLED=true`、`GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`、`GF_AUTH_DISABLE_LOGIN_FORM=true`、`GF_AUTH_BASIC_ENABLED=false`、`GF_USERS_ALLOW_SIGN_UP=false`。不启用 admin 登录，配置只从文件预置。
- 数据源 `editable: false`；看板 `allowUiUpdates: false`，不允许 UI 修改回写。
- 风险：Grafana 13 文档仍支持经典 JSON provisioning，V2 资源格式只对 dynamic dashboards 强制；实现时第一个任务做最小验证（数据源 + 单面板），失败再改 V2 或把 Grafana 固定到 `12.4.16`。回退版本会改变整个栈的版本，需要记录决策。

## Gin 指标中间件

- 在路由注册之前全局注册中间件，确保鉴权失败（401）的请求也被计入。
- 路由标签使用 `c.FullPath()`；未匹配返回空字符串时归入 `unmatched`。
- 状态码使用 `c.Writer.Status()` 的精确值。
- 计时从中间件进入开始，到 `c.Next()` 返回结束；包含鉴权、业务处理和写入 `ResponseWriter` 的序列化时间，不包含 socket 刷新和网络传输。慢客户端可能拉高该值，作为已知限制记录。
- `/readyz` 跳过，避免探针污染业务指标。

未采用第三方 Gin 指标库：当前只需要计数和直方图，自己实现约几十行，依赖更少，标签控制更直接。

## pgxpool 采集器

`pgxpool.Stat()` 提供上限、总数、空闲、使用中、建立中、获取次数、累计获取时长、无空闲等待次数、取消次数、新建和销毁计数。实现一个 `prometheus.Collector`，在 `Collect` 时读取当前快照。

限制：只有累计等待时长，没有等待时长直方图，因此只能计算平均等待，不能计算 P95 等待。不为此包装业务获取路径，避免侵入。

## 基准入口改动

- 轮数从 3 改为 5，同步命令行、测试和文档；`docs/payment-create-baseline.md` 标注旧 3 轮为历史协议。
- PostgreSQL 从动态端口改为固定 `55432`；入口检查 55432 和 9091，并持有 `<临时目录>/payment-hub-load.lock`（通常 `/tmp/payment-hub-load.lock`，由 `tempfile.gettempdir()` 决定）排他锁直到运行结束。锁是主机级、与 `--artifacts-dir` 无关，因为 `55432`/`9091` 是主机级固定资源，不同证据目录的运行同样不能并发。
- 每轮用本地密码文件创建只读角色；密码缺失或权限不对时拒绝运行；SQL 通过 stdin 传递，密码不进入 argv、`commands.log` 或摘要。
- 启动应用时设置 `PAYMENT_HUB_METRICS_ADDR=127.0.0.1:9091`；指标监听失败按配置语义使启动失败。
- 证据脚本按 `summary.json` 的 5 个 `load200` 阶段窗口判定，不按整次运行窗口。
- 原 3 轮实测记录标记为历史协议，不与 5 轮结果直接比较。

## 被否决的方案

| 方案 | 否决原因 |
|---|---|
| 只靠 k6 | 只有客户端视角，无法解释连接池、GC、数据库内部状态 |
| 常驻监控平台 | 单机单服务、无生产流量，维护成本高于收益 |
| cAdvisor | 应用是宿主机进程，cAdvisor 只能看到容器 |
| `pg_stat_statements` | 需要扩展和配置，带 SQL 文本，超出本期范围 |
| 远程写入/长期存储 | 增加依赖，当前只需要单轮回看 |
| 定制 k6 输出到 Prometheus | 需要自行编译 k6，收益低于直接抓取应用 |
| 动态端口 + 交接文件 | 每轮端口和凭据变化，exporter 需反复重启，时间窗断档 |
| 容器经 `host.docker.internal` 访问 | 到达的是网桥网关，不是宿主 loopback；应用和数据库绑 `127.0.0.1` 时不可达 |
| 直接把归档目录挂给 Prometheus 回看 | Prometheus 需要写锁并可能删除过期块，会修改证据 |
| 用 `pg_stat_activity_count` 查等待锁 | 带 `usename`、`application_name`，基数不可控；`pg_locks_count` 又没有 `granted` 标签 |
| 由基准入口检查全部五个端口 | 观测栈先启动时 9090/9187 已被占用，入口会拒绝自己的正常运行 |

## 未验证与风险

1. Grafana 13 对经典 JSON 看板的 provisioning 行为需要在实现时实测；失败则固定到 12.4 或改 V2 格式。
2. 观测栈自身 CPU 开销没有单独测量；通过“对比运行使用相同观测栈配置”抵消，绝对数值不与未启用观测栈的历史数据直接比较。
3. host 网络只支持 Linux/WSL2 同一来宾；Docker Desktop 不作为支持目标，文档必须在启动说明中写明。
4. 固定端口 55432、9091、9090、9187、3000 可能与本机其他进程冲突；按组件划分检查责任，并保留主机级运行锁（与 `--artifacts-dir` 无关，`55432`/`9091` 是主机级固定资源）。
5. `postgres_exporter` 的自定义查询使用已标记 deprecated 的 `--extend.query-path`；实现时先验证输出名称，失败则回退到 `stat_activity` 并重估基数。
6. `postgres_exporter` 的指标名称在版本间可能变化（例如 `_total` 后缀）；实现时以 v0.21.1 实际 `/metrics` 输出为准，并同步更新契约。
7. 回看副本需要与归档等量的磁盘；磁盘不足时回看失败，但不影响归档本身。

## 参考来源

- Prometheus 存储与 `--storage.tsdb.path`：https://prometheus.io/docs/prometheus/latest/storage/
- Prometheus 保留时间与 TSDB 写入：https://prometheus.io/docs/prometheus/latest/storage/#operational-aspects
- Docker host 网络：https://docs.docker.com/engine/network/drivers/host/
- Prometheus Go 应用指南：https://prometheus.io/docs/guides/go-application/
- Prometheus 指标实践：https://prometheus.io/docs/practices/instrumentation/
- Grafana provisioning：https://grafana.com/docs/grafana/latest/administration/provisioning/
- postgres_exporter README（v0.21.1 参数与权限）：https://github.com/prometheus-community/postgres_exporter
- postgres_exporter 指标定义（v0.21.1 `collector/`）：https://github.com/prometheus-community/postgres_exporter/tree/v0.21.1/collector
- Docker Hub 镜像标签：`prom/prometheus`、`grafana/grafana`、`prometheuscommunity/postgres-exporter`

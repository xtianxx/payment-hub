# 指标契约

**Feature**: `002-observability-metrics` | **Date**: 2026-11-26

本文件定义 Payment Hub 与观测栈对外暴露的指标名、标签、单位和查询口径。指标名与标签视为运维契约：变更时必须同步更新本文件、看板和文档。

## 命名规则

- 应用自定义指标统一使用 `payment_hub` 命名空间。
- 计数使用 `_total` 后缀；耗时使用 `_seconds`；字节使用 `_bytes`；比例不使用单位后缀；Gauge 不使用 `_total`。
- 标签只使用有限集合；不得包含订单号、支付 ID、请求 ID、接入方标识或原始 URL。
- Go 运行时和进程指标沿用 `client_golang` 的标准名称，不重命名。

## HTTP 指标

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `payment_hub_http_requests_total` | Counter | `method`、`route`、`status` | 请求完成时计数；`status` 为精确状态码字符串 |
| `payment_hub_http_request_duration_seconds` | Histogram | `method`、`route` | 从进入 API 路由组（含鉴权）到 handler 链返回；包含响应写入，不含 socket 刷新和网络传输 |

**标签取值**：

- `method`：当前为 `GET`、`POST`；其他方法归入固定值 `OTHER`，避免无界序列。
- `route`：Gin 的路由模板，当前为 `/api/v1/payments`、`/api/v1/payments/:payment_id`；未匹配路由为 `unmatched`。
- `status`：`200`、`201`、`400`、`401`、`404`、`409`、`500`、`503` 等有限集合。
- `/readyz` 不计入；指标端点在不同监听上，不进入本组指标。

**直方图桶（秒）**：

```text
0.001, 0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02, 0.025, 0.03, 0.04, 0.05, 0.075, 0.1, 0.25, 0.5, 1, 2.5, 5
```

覆盖当前约 10–25 ms 的 P95，并在 5–30 ms 区间保留足够分辨率；这 20 个是有限桶边界，文本暴露时每个 method-route 组合另有 1 个 `+Inf` 桶和 `_sum`/`_count`，共 23 条序列；当前最多 3 个已匹配组合和 3 个 `unmatched` 组合（GET/POST/OTHER），合计不超过 138 条。

**端点访问**：

- 仅 `GET /metrics` 成功；其他路径返回 `404`，其他方法返回 `405` 并带 `Allow: GET`（包括 `HEAD`）。
- 请求 `Host` 必须是回环字面量 IP 或 `localhost`（允许带端口）；其他 Host 返回 `403`，避免 DNS 重绑定页面借浏览器读取本地指标。
- 端点不做接入方鉴权，只依赖回环监听与上述 Host 限制；不得绑定非回环地址。

## 连接池指标

单个业务池，不添加池标签。

| 指标 | 类型 | 说明 |
|---|---|---|
| `payment_hub_db_pool_connections_max` | Gauge | 池上限 |
| `payment_hub_db_pool_connections` | Gauge | 已建立连接总数 |
| `payment_hub_db_pool_connections_idle` | Gauge | 空闲连接 |
| `payment_hub_db_pool_connections_in_use` | Gauge | 使用中连接（`AcquiredConns`） |
| `payment_hub_db_pool_connections_constructing` | Gauge | 正在建立的连接 |
| `payment_hub_db_pool_acquires_total` | Counter | 获取次数 |
| `payment_hub_db_pool_acquire_duration_seconds_total` | Counter | 累计获取等待时长 |
| `payment_hub_db_pool_empty_acquires_total` | Counter | 因无空闲连接而等待的次数；饱和的直接信号 |
| `payment_hub_db_pool_canceled_acquires_total` | Counter | 等待被取消的次数 |
| `payment_hub_db_pool_connections_created_total` | Counter | 新建连接次数 |
| `payment_hub_db_pool_connections_destroyed_max_idle_total` | Counter | 因空闲超时销毁的次数 |
| `payment_hub_db_pool_connections_destroyed_max_lifetime_total` | Counter | 因寿命到期销毁的次数 |

## Go 运行时与进程指标

| 指标 | 类型 | 说明 |
|---|---|---|
| `go_goroutines` | Gauge | 当前 goroutine 数 |
| `go_memstats_heap_inuse_bytes` | Gauge | 堆使用量 |
| `go_gc_duration_seconds` | Summary | GC 暂停分布（含分位数与 `_sum`/`_count`） |
| `process_cpu_seconds_total` | Counter | 进程 CPU 累计时间 |
| `process_resident_memory_bytes` | Gauge | 进程常驻内存 |

以上由 `client_golang` 的 Go collector 和 process collector 提供。

## PostgreSQL 指标

由 `postgres_exporter v0.21.1` 提供，命名空间 `pg`，数据库为 PostgreSQL 16.15。默认 collector 中有高基数或带用户标签的项，必须显式配置：

- 关闭：`settings`、`stat_activity`、`stat_activity_autovacuum`、`stat_user_tables`、`statio_user_tables`、`statio_user_indexes`、`stat_progress_vacuum`、`locks`、`database`、`replication`、`replication_slots`、`database_wraparound`、`long_running_transactions`、`process_idle`。
- 保留：`stat_database`、`stat_bgwriter`；`pg_up` 由 exporter 自身提供。
- 自定义查询：`deploy/observability/postgres-exporter-queries.yaml` 提供 `pg_benchmark_waiting_locks` 和 `pg_benchmark_granted_locks`，不带 `usename`、`application_name` 等标签。
- 该文件使用 v0.21.1 的 `--extend.query-path`（已标记 deprecated）；实现时必须验证输出名称，失败时不得退回 `stat_activity`（FR-028 禁止该 collector 及其用户标签），锁等待指标标记为 No data 并在证据与文档中记录限制。

| 指标 | 类型 | 关键标签 | 说明 |
|---|---|---|---|
| `pg_up` | Gauge | — | exporter 能否连接数据库 |
| `pg_stat_database_numbackends` | Gauge | `datname` | 当前连接数 |
| `pg_stat_database_xact_commit` | Counter | `datname` | 已提交事务数 |
| `pg_stat_database_xact_rollback` | Counter | `datname` | 已回滚事务数 |
| `pg_stat_database_blks_hit` | Counter | `datname` | 缓存命中的块数 |
| `pg_stat_database_blks_read` | Counter | `datname` | 从磁盘读取的块数 |
| `pg_stat_database_temp_files` | Counter | `datname` | 临时文件数 |
| `pg_stat_database_temp_bytes` | Counter | `datname` | 临时文件字节数 |
| `pg_stat_database_deadlocks` | Counter | `datname` | 死锁数 |
| `pg_benchmark_waiting_locks` | Gauge | — | 等待授予的锁数量；自定义查询 |
| `pg_benchmark_granted_locks` | Gauge | — | 已授予的锁数量；自定义查询 |
| `pg_stat_bgwriter_checkpoints_timed_total` | Counter | — | 定时检查点次数 |
| `pg_stat_bgwriter_checkpoints_req_total` | Counter | — | 请求触发的检查点次数 |
| `pg_stat_bgwriter_checkpoint_write_time_total` | Counter | — | 检查点写文件累计时间（毫秒） |

**注意**：

- `pg_stat_database_xact_commit` 等名称不带 `_total`，这是 exporter 的现有命名，不自行改写。
- 等待锁使用 `pg_benchmark_waiting_locks`；不使用 `pg_stat_activity_count`，避免把 `usename`、`application_name` 写入指标。
- `pg_stat_database` 带 `datname` 标签；当前实例通常只有 `postgres`、`template0`、`template1` 和当前测量库 4 个值。该 collector 实际还带 `datid`，它随数据库 OID 变化，但不含用户或业务标识。
- 不采集 `pg_stat_statements`、SQL 文本和用户级高基数标签。

## 查询契约

以下查询是验收和看板的基准口径。Grafana 中使用 `$__rate_interval`，文档和检查脚本使用 `[1m]`。

**抓取状态与数据库连接**

```promql
up{job="payment-hub"}
up{job="postgres"}
pg_up
```

| exporter | 数据库 | `up{job="postgres"}` | `pg_up` | 解释 |
|---|---|---|---|---|
| 停止 | — | 0 | 无样本 | exporter 抓取失败 |
| 运行 | 停止 | 1 | 0 | exporter 正常，数据库不可达 |
| 运行 | 运行 | 1 | 1 | 数据库可达 |

**请求速率**

```promql
sum(rate(payment_hub_http_requests_total[1m])) or vector(0)
```

从未有请求时没有序列，结果为 0；有历史序列但当前窗口无请求时 `rate` 为 0。应用停止时 `up=0`，不能只看速率判断。

**按路由的请求速率**

```promql
sum by (route, status) (rate(payment_hub_http_requests_total[1m]))
```

**5xx 比例**

```promql
(
  sum(rate(payment_hub_http_requests_total{status=~"5.."}[1m]))
  or vector(0)
)
/
(
  sum(rate(payment_hub_http_requests_total[1m]))
  > 0
)
```

无请求或窗口内计数器增量为 0 时结果为空（看板显示 No data），不返回 `NaN`；有请求但无 5xx 时为 0。

**4xx/5xx 速率**

```promql
sum(rate(payment_hub_http_requests_total{status=~"4.."}[1m])) or vector(0)
sum(rate(payment_hub_http_requests_total{status=~"5.."}[1m])) or vector(0)
```

**滚动 P95**

```promql
histogram_quantile(
  0.95,
  sum by (le, route) (rate(payment_hub_http_request_duration_seconds_bucket[1m]))
)
```

**整轮 P95（验收用）**

```promql
histogram_quantile(
  0.95,
  sum by (le, route) (increase(payment_hub_http_request_duration_seconds_bucket[$__range]))
)
```

与 k6 P95 对照时，差异容差为 ±3ms 或 ±20%（取较大者）；超出时需说明时间窗或桶边界，不把桶插值误差当成回归。

**GC 次数与平均暂停**

```promql
rate(go_gc_duration_seconds_count[1m])
rate(go_gc_duration_seconds_sum[1m]) / rate(go_gc_duration_seconds_count[1m])
```

累计分位数只作参考，不能按窗口聚合；判断“是否变频繁”看次数速率和平均暂停。

**连接池利用率**

```promql
payment_hub_db_pool_connections_in_use
/
payment_hub_db_pool_connections_max
```

**无空闲等待速率**

```promql
rate(payment_hub_db_pool_empty_acquires_total[1m])
```

**平均获取等待**

```promql
rate(payment_hub_db_pool_acquire_duration_seconds_total[1m])
/
(
  rate(payment_hub_db_pool_acquires_total[1m])
  > 0
)
```

无获取或窗口内增量为 0 时结果为空，不返回 `NaN`，也不伪装成 0。

**PostgreSQL 事务速率**

```promql
sum(rate(pg_stat_database_xact_commit[1m]))
```

**等待锁的会话数**

```promql
pg_benchmark_waiting_locks
```

由自定义查询提供，只统计 `pg_locks.granted = false`，不带 `usename`、`application_name`。

**缓存命中率**

```promql
sum(rate(pg_stat_database_blks_hit[1m]))
/
(
  (
    sum(rate(pg_stat_database_blks_hit[1m]))
    +
    sum(rate(pg_stat_database_blks_read[1m]))
  )
  > 0
)
```

分母为 0（窗口内没有块访问）时结果为空，不返回 `NaN`，也不显示 0。

**临时文件速率**

```promql
sum(rate(pg_stat_database_temp_files[1m]))
```

**检查点写入耗时速率（毫秒/秒）**

```promql
sum(rate(pg_stat_bgwriter_checkpoint_write_time_total[1m]))
```

## 显示口径

| 查询 | 抓取失败 | 抓取成功、无请求 | 抓取成功、有请求 |
|---|---|---|---|
| 请求速率 | 0（用 `up=0` 区分） | 0 | >0 |
| 4xx 速率 | 0（用 `up=0` 区分） | 0 | ≥0 |
| 5xx 速率 | 0（用 `up=0` 区分） | 0 | ≥0 |
| 5xx 比例 | No data | No data | 0 或 >0 |
| 平均获取等待 | No data | No data（无获取） | 0 或 >0 |
| 数据库事务速率 | No data（`pg_up=0`） | 0 或 No data | >0 |
| 等待锁 | No data | 0 | 0 或 >0 |
| 缓存命中率 | No data | No data（分母为 0） | 0–1 |

## 基数预算

- HTTP：请求序列不超过 24 条；直方图最多 6 个 method-route 组合（3 个已匹配 + GET/POST/OTHER 的 `unmatched`）× (20 个有限桶 + 1 个 `+Inf` 桶 + 2 条 sum/count) = 138 条。
- 连接池：12 条固定序列。
- Go/进程：约 30 条标准序列。
- PostgreSQL：`stat_database` 约 19 个指标 × 4 个 `datname`，`stat_bgwriter` 约 10 条，自定义锁查询 2 条，共约 90 条。
- 总量目标：一次运行少于 300 条时间序列（上述上限合计约 294 条）；不得随订单数量增长。

## 变更规则

- 新增或重命名指标、修改标签或桶边界，必须同时更新本文件、看板和 `docs/observability.md`。
- 不为单次排查临时增加标签；需要单笔信息时使用日志或 SQL，不进入指标。
- 指标名和标签的变更视为契约变更，需在提交说明中注明。

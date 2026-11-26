# 数据模型

**Feature**: `002-observability-metrics` | **Date**: 2026-11-26

本文件描述指标能力涉及的数据对象、标识、生命周期和证据格式。指标本身以 Prometheus 时间序列保存；这里定义的是运行、轮次、证据和目录之间的关系。

## 实体

### 运行标识（Run ID）

- 沿用现有 `summary.json` 的 12 位十六进制 `run_id`，例如 `a1b2c3d4e5f6`；不修改摘要 schema。
- 证据目录名为 `<UTC 时间戳>-<run_id>`，例如 `20261126T090000Z-a1b2c3d4e5f6`。
- 每次完整运行一个标识，关联该次运行的 k6 摘要、数据库证据、指标数据和指标证据标记。
- 测量轮标识为 `<run_id>r<N>`，`N` 为 1 到 5；与现有 `summary.json` 的 `round_id` 一致。

### 测量轮（Round）

- 一次完整运行包含 5 个测量轮；每轮依次执行冒烟、预热和 200 RPS × 120 秒测量。
- 每轮使用独立的 Compose 项目、数据库和卷；指标数据不按轮次切分文件。
- 每轮的测量阶段记录 `k6_started_epoch` 和 `k6_finished_epoch`，指标证据用这 5 个阶段窗口，而不是整次运行的起止时间。
- 冒烟、预热和轮间切换不计入证据窗口，避免把应用停止、数据库重建误判为抓取失败。

### 指标数据（Metrics Data）

- 物理形式为 Prometheus 本地 TSDB 目录，压测期间写入临时目录，结束后归档到 `<运行证据目录>/metrics/`。
- 逻辑形式为带名称、标签、时间戳和值的时间序列；契约见 [metrics.md](contracts/metrics.md)。
- 不包含订单号、支付 ID、请求 ID 或接入方标识。

### 指标证据标记（Metrics Evidence）

- 文件：`<运行证据目录>/metrics-evidence.json`。
- 由压测后的检查脚本生成；只描述指标是否完整，不改变基准资格。
- 推荐结构（每个已完成的测量阶段一条记录）：

```json
{
  "schema": "payment-hub.metrics-evidence.v2",
  "run_id": "a1b2c3d4e5f6",
  "run_dir": "20261126T090000Z-a1b2c3d4e5f6",
  "prometheus": "http://127.0.0.1:9090",
  "stages": [
    {
      "round": 1,
      "round_id": "a1b2c3d4e5f6r1",
      "window": {
        "start_utc": "2026-11-26T09:00:05Z",
        "end_utc": "2026-11-26T09:02:05Z"
      },
      "targets": {
        "payment-hub": "up",
        "postgres": "up",
        "pg_up": "up"
      },
      "scrapes": {
        "expected": 23,
        "observed": 23,
        "up_1": 23,
        "pg_up_1": 23
      },
      "required_series": {
        "payment_hub_http_requests_total": "present",
        "payment_hub_http_request_duration_seconds_bucket": "present",
        "go_goroutines": "present",
        "payment_hub_db_pool_connections_in_use": "present",
        "payment_hub_db_pool_acquires_total": "present",
        "payment_hub_db_pool_acquire_duration_seconds_total": "present",
        "payment_hub_db_pool_empty_acquires_total": "present",
        "pg_up": "present",
        "pg_stat_database_xact_commit": "present",
        "pg_stat_database_blks_hit": "present",
        "pg_benchmark_waiting_locks": "present",
        "pg_stat_bgwriter_checkpoints_timed_total": "present"
      },
      "status": "complete",
      "missing": []
    }
  ],
  "status": "complete",
  "missing": [],
  "checked_at_utc": "2026-11-26T09:22:00Z"
}
```

- `stages` 只包含实际运行的测量阶段；中断时未运行的阶段不写入，整体 `status` 为 `incomplete`。
- `scrapes.expected` 按 `max(0, floor(窗口秒数 / 5) - 1)` 计算，允许 1 个边界样本的容差；`observed` 用 `count_over_time(up[window])` 取得，用于发现 Prometheus 自身中断。
- `status` 只有 `complete` 和 `incomplete`；整体 `complete` 要求所有阶段 `complete`。
- `missing` 列出缺失的目标、`pg_up` 或序列；`incomplete` 时不得用于指标验收。
- 归档失败时额外记录 `archive_error`，整体仍为 `incomplete`，并保留临时目录供人工处理。

### 监控密码文件

- 路径由 `PAYMENT_HUB_MONITOR_PASSWORD_FILE` 指定，默认 `~/.config/payment-hub/monitor-password`。
- 内容为单行密码，权限必须是 `0600`；由基准入口在 preflight 检查一次。
- 基准入口每轮通过 stdin 执行 `CREATE ROLE postgres_exporter ... PASSWORD ...`，密码不进入 argv、日志或摘要。
- exporter 通过只读挂载读取同一文件，使用 `--datasource.pass-file`；容器以宿主 UID/GID 运行，exporter 不自行检查权限。
- 不写入仓库、迁移文件、容器环境变量或运行摘要。

### 看板配置

- Grafana 数据源和看板由 `deploy/observability/grafana/provisioning/` 下的文件预置。
- 看板 UID 固定，便于链接和版本管理。
- 看板只读：Grafana 匿名 Viewer，关闭 basic auth、登录表单和注册；数据源 `editable: false`，看板 `allowUiUpdates: false`，不允许 UI 修改后回写。
- 回看时数据源指向归档副本上的 Prometheus，不直接指向归档目录。

## 目录布局

一次完整运行的证据目录沿用基准入口现有结构：

```text
<运行证据目录>/                # <UTC 时间戳>-<run_id>
├── summary.json              # 基准摘要，含 5 轮的阶段时间窗
├── commands.log              # 命令记录，不含密码
├── round-1/
│   ├── logs/                 # 应用、k6、Compose 日志
│   ├── db/                   # 只读 SQL 对账结果
│   └── results/              # k6 摘要和阶段结果
├── round-2/ … round-5/       # 同上
├── metrics/                  # 归档的 Prometheus TSDB
├── metrics.tmp/              # 归档失败时保留的临时副本
└── metrics-evidence.json     # 指标证据标记
```

压测期间的临时指标目录由观察者指定，例如 `/tmp/opencode/payment-hub-metrics/<时间戳>/`；结束后先复制到 `metrics.tmp/`，校验后 rename 为 `metrics/`。归档失败时保留 `metrics.tmp/` 并写入 `archive_error`。

## 生命周期

### 运行

```text
prepared -> preflight(lock + ports) -> rounds 1..5 -> completed | incomplete
        -> metrics archived -> reviewed -> cleaned
```

- `completed` 由现有基准规则判定，与指标是否完整无关。
- 同一时间只允许一个运行：基准入口持有 `<artifacts root>/.payment-hub-load.lock` 的排他 `flock`，直到运行结束；端口检查作为辅助。
- 运行可能被中断；中断后只判定实际完成的测量阶段，不把计划时长当作已运行时长。
- `metrics archived` 表示指标目录已进入运行证据目录。
- `cleaned` 由观察者确认后手动执行；不自动删除。

### 指标证据

```text
not_checked -> complete | incomplete
```

- 每个测量阶段使用 `summary.json` 中 `load200` 阶段的 `k6_started_epoch` / `k6_finished_epoch` 作为窗口。
- 阶段内两个抓取目标的抓取次数不少于期望值、每次 `up=1`、`pg_up=1`，且关键序列存在，才记为 `complete`。
- 任一抓取失败、抓取次数不足或关键序列缺失，记为 `incomplete`，并保留缺失明细。
- 运行被中断时，未运行的阶段记为缺失，整体为 `incomplete`。

## 关系

- 1 个运行标识对应 1 个证据目录、1 份指标数据、1 个指标证据标记。
- 1 个运行包含 5 个测量轮；轮次通过阶段时间窗关联到同一份指标数据。
- 1 个看板对应 1 个 Prometheus 数据源；数据源指向归档的临时副本，不直接指向归档目录。

## 校验规则

- `metrics-evidence.json` 的 `run_id` 必须等于 `summary.json` 的 `run_id`；`run_dir` 必须等于证据目录名。
- 每个阶段窗口必须落在对应轮次的 `k6_started_epoch` 和 `k6_finished_epoch` 之间。
- `complete` 时所有阶段 `complete` 且 `missing` 为空；`incomplete` 时 `missing` 或失败阶段不得为空。
- 指标数据目录必须位于运行证据目录内，且不得包含凭据文件。
- 密码文件权限不是 `0600` 时，入口必须拒绝运行；密码不得出现在 argv、日志或摘要中。
- 55432/9091 被占用或运行锁已被持有：基准入口拒绝运行；9090/9187 被占用：观测栈启动失败；3000 被占用：回看启动失败。
- 同一时间只允许一个运行；不得共享或覆盖另一个运行的数据目录。
- 归档副本只能被一个 Prometheus 实例使用；副本已被占用时，回看必须拒绝启动。

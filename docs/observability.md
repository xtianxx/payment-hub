# 观测与指标证据

本文档说明本地压测期间如何启动观测栈、检查指标证据、归档 Prometheus 数据、回看看板、导出查询结果和清理资源。指标契约见 [specs/002-observability-metrics/contracts/metrics.md](../specs/002-observability-metrics/contracts/metrics.md)。

## 范围与边界

- 只服务本地压测归因：应用、连接池、PostgreSQL 的指标，以及 5 个测量阶段的完整性证据。
- 观测栈按需启动，不常驻、不公网、不告警、不做长期存储；只有 Linux/WSL2 同一来宾的 host 网络受支持，Docker Desktop 的独立虚拟机模式不支持。
- 不改变支付接口契约，不作为 CI 门禁或生产容量承诺。
- 指标不包含订单号、支付 ID、请求 ID 或用户标签；明细排查使用日志或 SQL。

## 前置条件

- Docker Engine 与 Compose，且当前用户在 `docker` 组或有等价权限。
- Python 3.9+、`psql`，以及已按基准文档准备的 k6、Go 和迁移镜像。
- 端口责任：基准入口检查 `55432` 和 `9091`；观测栈检查 `9090` 和 `9187`；`3000` 只在回看阶段占用。
- 同一时间只允许一个基准运行，由 `/tmp/payment-hub-load.lock` 的主机级排他锁保证。
- 磁盘足够存放 5 个数据库卷、Prometheus 数据和归档副本。

## 1. 准备凭据和容器身份

```bash
install -d -m 700 "$HOME/.config/payment-hub"
umask 077
openssl rand -hex 16 > "$HOME/.config/payment-hub/monitor-password"
chmod 600 "$HOME/.config/payment-hub/monitor-password"

export PAYMENT_HUB_MONITOR_PASSWORD_FILE="$HOME/.config/payment-hub/monitor-password"
export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"
```

密码文件必须是 `0600` 的单行文件，且首尾不能有空白（exporter 读取时会裁剪空白）。基准入口每轮通过 stdin 创建或更新只读角色 `postgres_exporter` 并授予 `pg_monitor`，密码不会进入 argv、`commands.log` 或 `summary.json`。

## 2. 启动观测栈（测量阶段）

测量期间只启动 Prometheus 和 postgres-exporter；Grafana 在回看阶段启动。

```bash
export PROMETHEUS_DATA_DIR="/tmp/opencode/payment-hub-metrics/$(date -u +%Y%m%dT%H%M%SZ)"
export PROMETHEUS_CONFIG_FILE="$PWD/deploy/observability/prometheus.yml"
mkdir -p "$PROMETHEUS_DATA_DIR"
chmod 700 "$PROMETHEUS_DATA_DIR"

docker compose -f deploy/compose.observability.yaml up -d prometheus postgres-exporter
```

显式设置 `PROMETHEUS_CONFIG_FILE` 指向测量配置，避免沿用 shell 中残留的回看配置。

`verify_observability.py` 需要基准数据库已经启动、`postgres_exporter` 角色已由基准入口创建，因此应在基准第一轮开始后另开终端执行：

```bash
python3 tests/benchmark/verify_observability.py --timeout 30
```

退出码：`0` 通过，`2` 传输错误，`3` 契约不满足。检查内容包括 `pg_up=1`、`pg_benchmark_waiting_locks`、`pg_stat_database_xact_commit`，以及 exporter 不暴露 `usename`/`application_name`。

## 3. 运行基准

按 [压测基准](payment-create-baseline.md) 执行 5 轮运行，并记下输出中的 `ARTIFACTS: <运行证据目录>`，后文称为 `$RUN_DIR`。入口会自动注入 `PAYMENT_HUB_METRICS_ADDR=127.0.0.1:9091`。

## 4. 检查指标证据

Prometheus 仍在运行时执行：

```bash
python3 tests/benchmark/check_metrics_evidence.py \
  --run-dir "$RUN_DIR" \
  --prometheus http://127.0.0.1:9090
echo "exit=$?"
```

退出码：`0` 全部阶段完整；`3` 存在不完整阶段；`2` 参数、summary 或 IO 错误；`4` Prometheus 不可达（仍会写出不完整证据和原因）。

结果写入 `$RUN_DIR/metrics-evidence.json`（schema `payment-hub.metrics-evidence.v2`），每个阶段记录：

- 时间窗与轮次；
- `targets`：`payment-hub`、`postgres`、`pg_up` 的 `up`/`down`/`absent`；
- `scrapes`：`expected`、`observed`、`up_1`、`exporter_observed`、`exporter_up_1`、`pg_up_1`；
- 15 项 `required_series` 是否 `present`；
- `status` 和 `missing`。

`expected = max(0, floor(窗口秒数 / 5) - 1)`；任一目标抓取次数不足、`up`/`pg_up` 出现 0、关键序列缺失或窗口内出现采样缺口，该阶段即为 `incomplete`。整体 `complete` 要求所有实际运行的阶段完整；`incomplete` 不影响基准资格，但不能用于指标验收。

## 5. 停止并归档指标数据

先停止 Prometheus，确保 WAL 落盘，再归档：

```bash
docker compose -f deploy/compose.observability.yaml down

python3 tests/benchmark/metrics_store.py archive \
  --source "$PROMETHEUS_DATA_DIR" \
  --run-dir "$RUN_DIR"
```

脚本校验源目录包含 `wal/`、复制到 `$RUN_DIR/metrics.tmp`、校验文件数与树哈希后原子重命名为 `$RUN_DIR/metrics`。退出码：`0` 成功，`2` 参数/前置条件/IO 错误，`3` 复制后校验失败。

- 归档失败时保留 `metrics.tmp/`，不删除、不覆盖已有 `metrics/`；在 `metrics-evidence.json` 中补充 `archive_error`（证据脚本会保留该字段并强制整体 `incomplete`）。
- 如果运行被中断，仍按同样步骤停止并归档；证据只判定实际完成的阶段。

## 6. 回看

回看使用归档副本，归档目录保持不变：

```bash
export VIEW_DIR="/tmp/opencode/payment-hub-view/$(date -u +%Y%m%dT%H%M%SZ)"

python3 tests/benchmark/metrics_store.py view \
  --run-dir "$RUN_DIR" \
  --view-dir "$VIEW_DIR"

export PROMETHEUS_DATA_DIR="$VIEW_DIR"
export PROMETHEUS_CONFIG_FILE="$PWD/deploy/observability/prometheus.view.yml"

docker compose -f deploy/compose.observability.yaml up -d prometheus grafana
```

打开 `http://127.0.0.1:3000`，选择预置看板“Payment Hub 压测总览”，时间范围选该次运行的时间窗。看板包含 HTTP 速率/错误率/P95、连接池利用率与等待、PostgreSQL 事务/锁等待/缓存/临时文件/检查点，以及 `up`/`pg_up` 抓取状态。

Grafana 为回环匿名只读：数据源 `editable: false`，看板 `allowUiUpdates: false`，关闭 basic auth、登录表单和注册。回看配置无抓取目标并设置较长保留时间，避免打开副本时删除过期块；同一副本只能被一个 Prometheus 实例使用。

本机已用 `grafana/grafana:13.2.7` 实测：数据源与看板自动加载、3000 只绑定回环。需要复核时运行：

```bash
python3 tests/benchmark/verify_grafana.py --timeout 90
```

该脚本用临时容器验证 provisioning，不拉取镜像，结束时只删除本次创建的容器。若 13.2.7 加载失败，先尝试 `--image grafana/grafana:12.4.16`，仍失败则评估改用 V2 看板格式，并记录决策。

## 7. 导出查询结果

```bash
python3 tests/benchmark/export_metrics.py \
  --run-dir "$RUN_DIR" \
  --prometheus http://127.0.0.1:9090 \
  --out "$RUN_DIR/metrics-export"
```

整体时间窗取所有 `load200` 阶段的最早开始到最晚结束；整轮 P95 的 `$__range` 会替换为该窗口秒数。每个契约查询写一份 CSV（`metric,labels,timestamp,value`），并写 `queries.json` 记录时间窗、PromQL 和样本数。

退出码：`0` 成功；`2` 参数、summary 或 IO 错误（输出目录已存在且非空会拒绝）；`4` Prometheus 不可达或查询失败（整体不落地）。

## 8. 清理

确认不再需要该次运行的数据后：

```bash
docker compose -f deploy/compose.observability.yaml down -v
rm -rf "$VIEW_DIR"

python3 - "$RUN_DIR" <<'PY'
import json, re, subprocess, sys

run_dir = sys.argv[1]
with open(f"{run_dir}/summary.json", encoding="utf-8") as f:
    summary = json.load(f)
run_id = str(summary.get("run_id", ""))
if re.fullmatch(r"[0-9a-f]{12}", run_id) is None:
    raise SystemExit(f"summary.json 的 run_id 不是 12 位十六进制：{run_id!r}")

for item in summary.get("overall", {}).get("volumes", []):
    volume = str(item.get("volume", ""))
    # 只删除本次运行的基准卷：名称必须同时带基准前缀和 run_id。
    if not volume.startswith("payment-hub-load-") or run_id not in volume:
        print("skipping unverified volume", volume)
        continue
    inspect = subprocess.run(
        ["docker", "volume", "inspect", volume],
        capture_output=True, text=True,
    )
    if inspect.returncode != 0:
        print("not found", volume)
        continue
    print("removing", volume)
    subprocess.run(["docker", "volume", "rm", volume], check=False)
PY
```

清理脚本只接受同时带 `payment-hub-load-` 前缀和本次 `run_id` 的卷名，并先用 `docker volume inspect` 确认存在；其他卷名只打印跳过，避免被篡改的 `summary.json` 删除无关数据。

- 观测栈数据目录 `$PROMETHEUS_DATA_DIR` 与回看副本 `$VIEW_DIR` 按需手动删除；归档目录 `$RUN_DIR/metrics` 不随 `down -v` 删除。
- 基准数据库卷必须按 `summary.json.overall.volumes` 逐个删除，不能把 `down -v` 当作基准卷清理。
- 证据目录和数据库卷不会自动删除；删除前确认该次运行已不再需要。

## 9. 已知限制

- Grafana 13 的经典 JSON 看板已实测加载通过；升级 Grafana 大版本后需重新运行 `verify_grafana.py`。
- 看板使用 `$__rate_interval` 与 `$__range`，与文档中的 `[1m]` 口径存在窗口差异，对比绝对值时以 `metrics-export/` 的记录为准。
- 采样缺口检测依赖 Prometheus 查询结果的评估时间点；Prometheus 自身中断表现为 `observed < expected`，两种信号都会使阶段 `incomplete`。
- 指标只覆盖契约列出的有限标签；订单级排查请使用日志和 SQL。

## 10. 修改后检查

修改指标、看板或基准入口后：

```bash
python3 -B -m unittest discover -s tests/benchmark -p 'test_*.py'
go test ./...
git diff --check
```

## 相关文档

- [压测基准](payment-create-baseline.md)
- [指标契约](../specs/002-observability-metrics/contracts/metrics.md)
- [快速开始](../specs/002-observability-metrics/quickstart.md)

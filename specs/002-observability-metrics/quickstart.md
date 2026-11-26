# 快速开始

**Feature**: `002-observability-metrics` | **Date**: 2026-11-26

本流程在本地跑一次完整压测（5 个测量轮），采集应用与 PostgreSQL 指标，归档后用 Grafana 回看。所有端口只绑定回环，观测栈使用 host 网络。

## 当前实现范围（MVP）

本文件描述目标形态的完整流程。当前完成 T001–T034（US1–US3）：HTTP、连接池和 PostgreSQL 指标已实现；观测栈 Compose、固定 `55432`、5 轮基准、运行锁和监控密码文件均已落地，设置 `PAYMENT_HUB_METRICS_ADDR` 后可从 `/metrics` 读取应用指标。仍未实现：`check_metrics_evidence.py` 与 `export_metrics.py` 仍是占位脚本，归档/回看脚本化（T041）和 Grafana 看板内容（T042）待完成。因此第 4、7 步不可用；第 2、3、5、6 步可按本文执行，回看时看板仍为占位。

## 前置条件

- Linux 或 WSL2 同一来宾的 Docker Engine；host 网络只在该形态下可靠，不支持 Docker Desktop 的独立虚拟机模式。
- 项目现有压测环境：Go、Python 3.9+、k6 v2.4.0、`psql`、`taskset`、Docker/Compose。
- 已准备与当前 `migrations/` 一致的本地迁移镜像，例如 `payment-hub-migrate:load-local`。
- 固定端口及检查责任：
  - `55432`（基准 PostgreSQL）和 `9091`（应用指标）：基准入口检查；
  - `9090`（Prometheus）和 `9187`（exporter）：观测栈 Compose 检查；
  - `3000`（Grafana）：只在回看阶段检查。
- 同一时间只能有一个运行；基准入口用 `/tmp/payment-hub-load.lock`（固定路径，与 `TMPDIR`/`TEMP`/`TMP` 无关）排他锁保证。锁是主机级、与 `--artifacts-dir` 无关，因为 `55432`/`9091` 是主机级固定资源，不同证据目录的运行同样不能并发。
- 磁盘足够保留一次运行的 5 个数据库卷和指标数据。

## 1. 准备监控密码文件和容器身份

```bash
install -d -m 700 "$HOME/.config/payment-hub"
umask 077
openssl rand -hex 16 > "$HOME/.config/payment-hub/monitor-password"
chmod 600 "$HOME/.config/payment-hub/monitor-password"
export PAYMENT_HUB_MONITOR_PASSWORD_FILE="$HOME/.config/payment-hub/monitor-password"
export HOST_UID=$(id -u)
export HOST_GID=$(id -g)
```

密码只用于本地只读监控角色 `postgres_exporter`。Prometheus 和 exporter 以 `HOST_UID:HOST_GID` 运行，密码文件以只读方式挂载；权限由基准入口检查，容器内不检查。运行期间不要修改该文件。

## 2. 启动压测阶段的观测栈

测量期间只启动 Prometheus 和 exporter，不启动 Grafana，避免额外 CPU 竞争。

```bash
docker network inspect host >/dev/null
export PROMETHEUS_DATA_DIR="/tmp/opencode/payment-hub-metrics/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$PROMETHEUS_DATA_DIR"

docker compose -f deploy/compose.observability.yaml up -d prometheus postgres-exporter
```

`docker network inspect host` 只验证 host 网络存在；Docker Desktop 的独立虚拟机不满足本方案前提。数据目录必须属于 `HOST_UID`，否则 Prometheus 无法写 WAL。

确认两个端点可访问：

```bash
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:9187/metrics | grep -E '^pg_up|^pg_benchmark_waiting_locks' || true
```

`pg_up` 在基准数据库启动前为 `0` 属于正常；压测开始后应为 `1`。`pg_benchmark_waiting_locks` 由自定义查询提供，数据库就绪后才会出现；如果名称一直不存在，按 `contracts/metrics.md` 的说明检查 `--extend.query-path`。

9091 此时还不存在，应用启动后才会监听。

## 3. 运行基准

```bash
python3 -B tests/benchmark/run_payment_create.py \
  --migrate-image payment-hub-migrate:load-local \
  --profile 200 --rounds 5
```

入口会：

- 获取主机级运行锁（与 `--artifacts-dir` 无关），检查 `55432` 和 `9091`，冲突时立即失败；
- 使用固定 `55432` 端口和 5 个测量轮；
- 每轮通过 stdin 创建只读角色 `postgres_exporter`，密码不进入 argv 或日志；
- 启动应用时设置 `PAYMENT_HUB_METRICS_ADDR=127.0.0.1:9091`；
- 在结束时打印 `ARTIFACTS: <运行证据目录>`。

记下这个目录，后续称为 `$RUN_DIR`。

## 4. 检查指标证据

在 Prometheus 仍在运行时执行。脚本读取 `summary.json` 中 5 个 `load200` 阶段的窗口，按阶段写出 `metrics-evidence.json`：

```bash
python3 tests/benchmark/check_metrics_evidence.py \
  --run-dir "$RUN_DIR" \
  --prometheus http://127.0.0.1:9090
echo "exit=$?"
```

退出码：

- `0`：全部测量阶段完整；
- `3`：存在不完整阶段；
- `2`：参数或文件 IO 错误；
- `4`：Prometheus 不可达；脚本仍会写入不完整证据和原因。

查看结果：

```bash
python3 -m json.tool "$RUN_DIR/metrics-evidence.json"
```

每个阶段记录 `up`、`pg_up`、抓取次数与期望次数、缺失序列。整体 `status` 为 `complete` 才可用于指标验收；`incomplete` 不影响基准资格。

## 5. 停止并归档指标数据

```bash
set -euo pipefail
docker compose -f deploy/compose.observability.yaml down

test -d "$PROMETHEUS_DATA_DIR"
test ! -e "$RUN_DIR/metrics"
test ! -e "$RUN_DIR/metrics.tmp"

mkdir -p "$RUN_DIR/metrics.tmp"
if ! cp -a "$PROMETHEUS_DATA_DIR"/. "$RUN_DIR/metrics.tmp/"; then
  echo "archive copy failed; keeping $RUN_DIR/metrics.tmp" >&2
  exit 1
fi
test -d "$RUN_DIR/metrics.tmp/wal"
du -sh "$RUN_DIR/metrics.tmp"
mv "$RUN_DIR/metrics.tmp" "$RUN_DIR/metrics"
```

先停止 Prometheus 再复制，确保 WAL 已落盘。复制的每一步失败都会在 rename 前退出，保留 `metrics.tmp/` 供人工处理；只有复制成功且 `wal/` 存在时才原子重命名为 `metrics/`。失败时在 `metrics-evidence.json` 记录 `archive_error`，不要删除临时目录。

如果运行被中断，仍按同样步骤停止并归档；证据只判定实际完成的测量阶段，未运行的阶段记为缺失。

## 6. 回看

回看使用归档副本，归档目录保持不变：

```bash
export VIEW_DIR="/tmp/opencode/payment-hub-view/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$VIEW_DIR"
cp -a "$RUN_DIR/metrics"/. "$VIEW_DIR"/

export PROMETHEUS_DATA_DIR="$VIEW_DIR"
export PROMETHEUS_CONFIG_FILE="$PWD/deploy/observability/prometheus.view.yml"

docker compose -f deploy/compose.observability.yaml up -d prometheus grafana
```

打开 `http://127.0.0.1:3000`，选择预置看板“Payment Hub 压测总览”，时间范围选该次运行的时间窗。Grafana 为回环匿名只读，已关闭 basic auth 和登录表单，不需要登录。

回看配置使用空抓取目标，并设置较长的保留时间，避免 Prometheus 打开归档副本时删除过期块。同一个副本只能被一个 Prometheus 实例使用。

## 7. 导出查询结果

```bash
python3 tests/benchmark/export_metrics.py \
  --prometheus http://127.0.0.1:9090 \
  --run-dir "$RUN_DIR" \
  --out "$RUN_DIR/metrics-export"
```

导出内容包括接口速率、5xx 比例、整轮 P95、连接池利用率和等待、PostgreSQL 事务与锁等待等契约查询。`metrics-export/` 下每个查询一份 CSV，并有一份记录时间窗和 PromQL 的 JSON 汇总。

退出码：`0` 成功；`2` 参数或 IO 错误；`4` Prometheus 不可达。

## 8. 清理

确认不再需要该次运行的数据后：

```bash
docker compose -f deploy/compose.observability.yaml down -v

python3 - "$RUN_DIR" <<'PY'
import json, subprocess, sys

run_dir = sys.argv[1]
with open(f"{run_dir}/summary.json", encoding="utf-8") as f:
    summary = json.load(f)
for item in summary.get("overall", {}).get("volumes", []):
    volume = item.get("volume")
    if volume:
        print("removing", volume)
        subprocess.run(["docker", "volume", "rm", volume], check=False)
PY

docker volume ls | grep 'payment-hub-load' || true
rm -rf "$RUN_DIR" "$VIEW_DIR"
```

`down -v` 只清理观测栈自己的卷；基准的数据库卷必须按 `summary.json` 的 `overall.volumes` 逐个删除。删除后核对 `docker volume ls`，再删除证据目录和回看副本。

## 常见问题

| 现象 | 检查 |
|---|---|
| `up{job="payment-hub"}=0` | 应用是否设置了 `PAYMENT_HUB_METRICS_ADDR`；`curl http://127.0.0.1:9091/metrics` 是否可访问；9091 是否被占用 |
| `up{job="postgres"}=0` | exporter 是否启动；`curl http://127.0.0.1:9187/metrics` 是否可访问 |
| `up{job="postgres"}=1` 但 `pg_up=0` | exporter 在运行，但连不上数据库；检查 55432 和数据库容器是否就绪 |
| 看板显示 No data | 时间窗是否选对；该时间段是否真的没有请求；不要把它当成 0 |
| Grafana 数据源不可达 | 检查 `http://127.0.0.1:9090` 是否可访问；看板显示错误或 No data，不当作 0 |
| 5xx 比例为 0 | 有请求但没有 5xx，属于正常的 0；无请求时应为 No data |
| 归档失败或磁盘不足 | 保留 `metrics.tmp/`，证据记录 `archive_error`；不要删除已有证据或临时目录 |
| 运行数据保留多久 | 保留到观察者确认清理；不设自动保留上限，磁盘不足时先停止归档 |
| 端口冲突 | 基准入口只管 55432/9091；9090/9187 冲突看 Compose 报错；3000 只在回看阶段占用 |
| 密码文件被拒绝 | 权限必须是 `0600`，内容为单行非空密码；入口只检查一次 |
| exporter 读不到密码 | 确认已导出 `HOST_UID`/`HOST_GID`，且 Compose 以该身份挂载只读文件 |
| Prometheus 无法写数据目录 | 数据目录必须属于 `HOST_UID`；回看副本同理 |
| 运行被中断 | 证据只判定已完成的阶段，整体标记 `incomplete`；未运行阶段记为缺失 |
| 第二个运行无法启动 | 查看 `/tmp/payment-hub-load.lock`（固定路径）的持有者；锁是主机级、与 `--artifacts-dir` 无关，因为 `55432`/`9091` 是主机级固定资源，不同证据目录的运行也不能并发 |

## 限制

- 观测栈自身开销没有单独测量；需要对比的两次运行必须使用相同的观测栈配置。
- 不采 cAdvisor 容器指标和 `pg_stat_statements`；应用作为宿主机进程运行。
- 服务端耗时包含 handler 内的响应写入，慢客户端可能拉高该值；分析时与 k6 客户端耗时一起看。
- 不提供告警、长期存储、公网访问或自动清理。
- 旧 3 轮实测记录属于历史协议，不与 5 轮结果直接比较。

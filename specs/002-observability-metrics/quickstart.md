# 快速开始

**Feature**: `002-observability-metrics` | **Date**: 2026-11-26

本流程在本地跑一次完整压测（5 个测量轮），采集应用与 PostgreSQL 指标，归档后用 Grafana 回看。所有端口只绑定回环，观测栈使用 host 网络。

## 当前实现范围（MVP）

本文件命令与当前实现逐条对齐，并在 Linux 环境完整实跑验证过。HTTP、连接池和 PostgreSQL 指标、观测栈、固定 `55432`、5 轮基准、运行锁、监控密码、证据检查、导出、归档/回看脚本、Grafana 看板和指标基数测试均已落地。实跑记录与未验证边界见文末“实跑记录”。

## 前置条件

- Linux 或 WSL2 同一来宾的 Docker Engine；host 网络只在该形态下可靠，不支持 Docker Desktop 的独立虚拟机模式。
- 项目现有压测环境：Go、Python 3.9+、k6 v2.4.0、`psql`、`taskset`、Docker/Compose。
- Go 必须是满足 `go.mod` 的本地工具链：入口以 `GOTOOLCHAIN=local` 构建，`go version` 在默认 `auto` 下可能显示自动下载的版本，不能用它判断。用 `GOTOOLCHAIN=local go version` 核对；不满足时用 `--go` 指向符合版本的工具链二进制。
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

stat -c '%u %g %a %n' "$PAYMENT_HUB_MONITOR_PASSWORD_FILE"
```

密码只用于本地只读监控角色 `postgres_exporter`。Prometheus 和 exporter 以 `HOST_UID:HOST_GID` 运行，密码文件以只读方式挂载；权限由基准入口检查，容器内不检查。运行期间不要修改该文件。

## 2. 启动压测阶段的观测栈

测量期间只启动 Prometheus 和 exporter，不启动 Grafana，避免额外 CPU 竞争。

```bash
docker network inspect host >/dev/null
install -d -m 700 /tmp/opencode/payment-hub-metrics
export PROMETHEUS_DATA_DIR="$(mktemp -d /tmp/opencode/payment-hub-metrics/run-XXXXXX)"
export PROMETHEUS_CONFIG_FILE="$PWD/deploy/observability/prometheus.yml"
stat -c '%u %g %a %n' "$PROMETHEUS_DATA_DIR"

docker compose -f deploy/compose.observability.yaml up -d prometheus postgres-exporter
```

`docker network inspect host` 只验证 host 网络存在；Docker Desktop 的独立虚拟机不满足本方案前提。数据目录用 `mktemp -d` 生成唯一路径（权限 `700`），不要复用旧目录，否则 Prometheus 会沿用已有 TSDB，下一次归档会混入历史序列。目录必须属于 `HOST_UID`，否则 Prometheus 无法写 WAL；`stat` 输出的 UID/GID 必须等于 `HOST_UID`/`HOST_GID`。

必须显式设置 `PROMETHEUS_CONFIG_FILE`：测量阶段使用抓取配置 `deploy/observability/prometheus.yml`，回看阶段再切换为 `prometheus.view.yml`，避免 shell 中残留的上一次配置。

确认两个端点可访问：

```bash
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:9187/metrics | grep -E '^pg_up|^pg_benchmark_waiting_locks' || true
```

`pg_up` 在基准数据库启动前为 `0` 属于正常；压测开始后应为 `1`。`pg_benchmark_waiting_locks` 由自定义查询提供，数据库就绪后才会出现；如果名称一直不存在，按 `contracts/metrics.md` 的说明检查 `--extend.query-path`。

指标契约验收放在基准第一轮开始之后（`postgres_exporter` 角色由基准入口创建，数据库也已就绪），另开终端执行：

```bash
python3 tests/benchmark/verify_observability.py --timeout 30
echo "exit=$?"
```

退出码 `0` 通过、`2` 传输错误、`3` 契约不满足；检查 `pg_up=1`、`pg_benchmark_waiting_locks`、`pg_stat_database_xact_commit`，并确认 exporter 不暴露 `usename`/`application_name`。

9091 此时还不存在，应用启动后才会监听。

## 3. 运行基准

```bash
# 先确认本地工具链本身满足 go.mod（不受 GOTOOLCHAIN=auto 影响）。
GOTOOLCHAIN=local go version

python3 -B tests/benchmark/run_payment_create.py \
  --migrate-image payment-hub-migrate:load-local \
  --k6 "$(command -v k6)" \
  --go "$(command -v go)" \
  --profile 200 --rounds 5
```

如果 `GOTOOLCHAIN=local go version` 低于 `go.mod` 要求，把 `--go` 换成满足版本的二进制路径；入口不会自动下载工具链。

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
docker compose -f deploy/compose.observability.yaml down

python3 tests/benchmark/metrics_store.py archive \
  --source "$PROMETHEUS_DATA_DIR" \
  --run-dir "$RUN_DIR"
```

先停止 Prometheus 再归档，确保 WAL 已落盘。脚本校验源目录包含 `wal/`、复制到 `$RUN_DIR/metrics.tmp`，校验文件数与树哈希后原子重命名为 `$RUN_DIR/metrics`；退出码 `0` 成功、`2` 参数/前置条件/IO 错误、`3` 校验失败。失败时保留 `metrics.tmp/` 供人工处理，并在 `metrics-evidence.json` 记录 `archive_error`；不要删除临时目录。

如果运行被中断，仍按同样步骤停止并归档；证据只判定实际完成的测量阶段，未运行的阶段记为缺失。

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

`metrics_store.py view` 复制前后校验归档哈希不变，副本已存在且非空时拒绝；归档目录本身是唯一长期保留的数据，回看只读副本。`archive` 与 `view` 都会打印“源目录树哈希/目标目录树哈希”和文件数：回看时输出的归档哈希必须与归档时一致，两次输出可直接对照。不要用自写脚本比较哈希，算法包含路径边界和逐文件摘要，手写版本会与脚本不一致。

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

if python3 - "$RUN_DIR" <<'PY'
import json, re, subprocess, sys

run_dir = sys.argv[1]
with open(f"{run_dir}/summary.json", encoding="utf-8") as f:
    summary = json.load(f)
run_id = str(summary.get("run_id", ""))
if re.fullmatch(r"[0-9a-f]{12}", run_id) is None:
    raise SystemExit(f"summary.json 的 run_id 不是 12 位十六进制：{run_id!r}")

failed = []
for item in summary.get("overall", {}).get("volumes", []):
    round_number = item.get("round")
    expected = f"payment-hub-load-{run_id}r{round_number}_postgres_data"
    volume = str(item.get("volume", ""))
    # 卷名必须与本次运行和轮次精确对应，不接受前缀/子串匹配。
    if volume != expected:
        print("skipping unverified volume", volume, "expected", expected)
        failed.append(volume)
        continue
    inspect = subprocess.run(
        ["docker", "volume", "inspect", volume],
        capture_output=True, text=True,
    )
    if inspect.returncode != 0:
        if "no such volume" in (inspect.stderr or "").lower():
            print("absent", volume)
            continue
        print("inspect failed", volume, (inspect.stderr or "").strip())
        failed.append(volume)
        continue
    removed = subprocess.run(
        ["docker", "volume", "rm", volume],
        capture_output=True, text=True,
    )
    if removed.returncode != 0:
        print("remove failed", volume, (removed.stderr or "").strip())
        failed.append(volume)
    else:
        print("removed", volume)

for index, item in enumerate(summary.get("rounds", []), start=1):
    project = f"payment-hub-load-{run_id}r{item.get('round', index)}"
    containers = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", f"label=com.docker.compose.project={project}",
            "--format", "{{.Names}}",
        ],
        capture_output=True, text=True,
    )
    if containers.returncode != 0:
        print("container check failed", project, (containers.stderr or "").strip())
        failed.append(project)
        continue
    names = containers.stdout.split()
    if names:
        print("containers remain", project, names)
        failed.append(project)

if failed:
    raise SystemExit("清理未全部确认：%s" % failed)
PY
then
  docker volume ls | grep 'payment-hub-load' || true
  rm -rf "$RUN_DIR" "$VIEW_DIR"
else
  echo "卷或容器清理未全部确认，保留 $RUN_DIR 以便重试" >&2
fi
```

`down -v` 只清理观测栈自己的卷；基准的数据库卷必须按 `summary.json` 的 `overall.volumes` 逐个删除。脚本要求卷名精确等于 `payment-hub-load-{run_id}r{轮次}_postgres_data`，区分“确认不存在”和 inspect 失败，检查 `docker volume rm` 的退出码，并按 `com.docker.compose.project` 标签检查各轮是否还有残留容器。任何一步未确认成功都会以非零状态结束并保留 `$RUN_DIR`；只有全部确认后才删除证据目录和回看副本。

第 6 步把 `PROMETHEUS_DATA_DIR` 覆盖成了回看副本路径；测量阶段的数据目录在归档成功、回看副本也已删除后即可按记录的原路径删除，`$RUN_DIR/metrics` 是唯一长期保留的数据。

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

## 实跑记录

2026-11-26 在 Linux 16 vCPU 环境完整执行本流程一次（guest CPU 亲和：k6 `0-1`、PostgreSQL `2-5`、应用 `6-9`）：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 观测栈 | `docker compose ... up -d prometheus postgres-exporter` | 9090 就绪；exporter `pg_up=0`（数据库未启动，属预期） |
| 契约验收 | `verify_observability.py --timeout 30` | 第一轮数据库就绪后退出 0；`pg_up=1`、`pg_benchmark_waiting_locks` 与 `pg_stat_database_xact_commit` 可用，无 `usename`/`application_name` 标签 |
| 全量测试 | `go test ./...`；`python3 -B -m unittest discover -s tests/benchmark -p 'test_*.py'` | 退出 0：Go 2079 通过 / 10 个包；benchmark 143 项 OK；本阶段无测试失败需要修复 |
| 基数测试 | `go test ./internal/metrics/ -run StaysWithin -v` | 1 项通过；按导出序列统计（直方图桶 + `_sum`/`_count`）为 102 条，低于 300 预算 |
| 基准 | `run_payment_create.py --profile 200 --rounds 5` | `RESULT: PASS`，5 轮全部 `complete`，每轮 24000–24001 次迭代，`dropped_iterations=0`；测量轮 P95 18.1/30.9/23.7/18.0/27.0 ms，P99 23.6/56.3/28.0/20.8/53.8 ms |
| 证据 | `check_metrics_evidence.py` | 退出 0，`status=complete`；5 个阶段目标均为 `up`，全部关键序列 present，`expected` 23 与 `observed` 24–25 的抓取计数正常 |
| 归档 | `metrics_store.py archive`（先 `down` 观测栈） | 退出 0；源/目标树哈希一致，3 个文件，`metrics-evidence.json` 字节不变 |
| 回看 | `metrics_store.py view` + `up prometheus grafana` | 退出 0，归档与副本树哈希一致；Grafana 13.2.7 `/api/health` 正常、数据源与看板“Payment Hub 压测总览”自动加载，3000/9090 仅监听回环 |
| 归档数据 | Prometheus API 查询归档副本 | 第一轮窗口末：`sum(payment_hub_http_requests_total)=25013`，整轮 P95（121s）约 17.4 ms，`pg_up=1` |
| 导出 | `export_metrics.py --out "$RUN_DIR/metrics-export"` | 退出 0，17 个查询 + `queries.json`，CSV 为 CRLF，`metric` 列按查询名回退 |
| 清理 | `down -v` + 按 `overall.volumes` 删除 | 实跑时按前缀 + `run_id` 删除本次 5 个基准卷，`docker volume ls`、`docker ps -a` 无残留，其他历史卷未受影响；提交前收紧为卷名精确匹配并对 inspect/rm 失败、容器标签检查做了合成用例验证（精确卷缺失→`absent` 且退出 0，不匹配卷名→退出非 0，无关卷保留） |

未验证与限制：

- 首次基准调用失败：`--go "$(command -v go)"` 指向 `GOTOOLCHAIN=auto` 下显示的版本，实际本地工具链为 1.26.5，入口以 `GOTOOLCHAIN=local` 构建时拒绝；改用 1.27.4 工具链二进制重跑通过。已在前置条件和第 3 步补充核对方法。
- 第一次归档在观测栈仍在运行时执行（`down` 因缺少 `HOST_UID` 失败），随后删除该次归档、正确停止后用干净数据重新归档；本节结果来自重做后的归档。
- Grafana 仅通过 HTTP API 验证健康、数据源、看板加载与端口绑定，未逐面板人工目检；数据正确性用 Prometheus API 直接查询归档副本核对。
- CPU 亲和只约束 guest 内竞争，不代表 Windows 宿主物理隔离；单机单次运行，未做跨机或多配置对比。
- 未验证 Docker Desktop 独立虚拟机形态。
- 指标栈自身开销未单独测量，本记录不能用作不同观测配置之间的性能对比。

## 限制

- 观测栈自身开销没有单独测量；需要对比的两次运行必须使用相同的观测栈配置。
- 不采 cAdvisor 容器指标和 `pg_stat_statements`；应用作为宿主机进程运行。
- 服务端耗时包含 handler 内的响应写入，慢客户端可能拉高该值；分析时与 k6 客户端耗时一起看。
- 不提供告警、长期存储、公网访问或自动清理。
- 旧 3 轮实测记录属于历史协议，不与 5 轮结果直接比较。

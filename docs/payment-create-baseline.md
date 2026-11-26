# 创建支付：本地 k6 性能基准

这套基准用于回答：**在相同负载和环境下，改动后的创建支付有没有变慢？** 它只测 `POST /api/v1/payments` 的 Mock 成功路径，不混入查询、重试、渠道故障或真实资金操作。它不是 CI 门禁、生产容量承诺，也不诊断慢 `COMMIT`。

> 历史说明：本文第 6 节的三轮实测记录来自旧协议（3 轮、动态端口、无指标栈），仅作历史留存，不能与新协议（5 轮、固定 55432、指标采集）的结果直接比较。

## 固定条件

| 项目 | 基准配置 |
|---|---|
| 测量 | 固定到达率 200 次/秒 × 120 秒 × 5 轮；一迭代一笔支付，预分配和最大 VU 均为 128 |
| 每轮准备 | 独立新数据库及命名卷；先 2 RPS × 5 秒冒烟，再 50 RPS × 20 秒预热；冒烟、预热、测量用不同订单前缀 |
| CPU | WSL2 guest：k6 `0–1`、PostgreSQL `2–5`、应用 `6–9`；要求同核线程不拆组，检查有效掩码，不匹配不跑 |
| 数据库和应用 | 固定 PostgreSQL 镜像和迁移文件；入口自动使用 `paymenthub_benchmark` 构建标签固定 `MaxConns=16`；不更改普通应用默认池、不改 WAL 或持久化参数，不开详细查询日志 |
| 对账与收尾 | 逐轮只读 SQL 对账；不自动补跑。停止本轮服务并保留命名卷；确认不再需要后另行处理卷 |

CPU 分组隔离的是 WSL2 guest 中的直接竞争，不保证 Windows 宿主物理隔离。第一版从新库加固定预热起跑，不代表积累大量订单后的延迟。

## 手动重跑

从仓库根目录运行。前置条件：Ubuntu WSL2 或具有相同 CPU 拓扑的 Linux、本地 Docker/Compose、`psql`、Python 3.9+、`taskset`、符合 `go.mod` 的已安装 Go、已安装的 k6 v2.4.0，以及已在本地的固定 PostgreSQL 镜像和与当前 `migrations/` 对应的迁移镜像。入口不安装工具、不自动拉镜像；需要另行准备镜像时，先核对其来源和迁移版本。

首次运行前，在入口使用的 Go 环境中联网执行一次 `go mod download` 预热模块缓存；入口以 `GOPROXY=off` 构建应用，缓存未准备时会在测量前失败。k6 版本必须是 v2.4.0，其他版本会被拒绝；Docker 守护进程须与 k6 和应用处于同一 CPU 域（可见 CPU 数一致），否则入口拒绝起跑。

```bash
python3 -B tests/benchmark/run_payment_create.py --check-cpu-plan

# 使用已存在的本地迁移镜像。
docker image inspect payment-hub-migrate:load-local >/dev/null

python3 -B tests/benchmark/run_payment_create.py \
  --migrate-image payment-hub-migrate:load-local \
  --k6 "$(command -v k6)" \
  --go "$(command -v go)" \
  --profile 200 --rounds 5
```

如 `PATH` 上的 Go 或 k6 不是预期版本，用 `--go`、`--k6` 传入已验证的绝对路径。CPU 预检不启动 Docker；实际运行仍会检查容器及进程掩码、迁移文件和运行时池配置。使用独立 Compose 项目及固定的 `127.0.0.1:55432` 数据库端口，不连接现有 PostgreSQL。入口在启动前检查 `55432` 和 `9091`，并在 `<临时目录>/payment-hub-load.lock`（通常 `/tmp/payment-hub-load.lock`，由 `tempfile.gettempdir()` 决定）上持有排他锁；锁或端口冲突时立即失败。锁是主机级、与 `--artifacts-dir` 无关，因为 `55432`/`9091` 是主机级固定资源，不同证据目录的运行同样不能并发。监控密码文件由 `PAYMENT_HUB_MONITOR_PASSWORD_FILE` 指定（默认 `~/.config/payment-hub/monitor-password`），必须是 `0600` 的单行文件。原始日志和未脱敏产物不要入库。

运行结束时，`ARTIFACTS` 行会给出证据目录，默认位于 `/tmp/opencode/payment-hub-benchmark/load-runs/`。如需长期保留，用 `--artifacts-dir` 指定仓库外的持久目录。对比前核对 `summary.json` 中的源码 SHA 和工作树状态；有未提交改动时，不把 SHA 单独当作实际源码身份。

如果缺本地迁移镜像，可另行执行 `docker build --platform linux/amd64 -f deploy/Dockerfile.migrate -t payment-hub-migrate:load-local .`。此准备命令可能访问网络，不是基准入口的一部分。已有其他本地标签时，也可以直接用 `--migrate-image` 指定；入口会核对镜像中的迁移文件。

## 修改入口后的检查

修改 `tests/benchmark`、`internal/app/app.go` 的基准钩子或 `tests/benchmark/compose.benchmark.yaml` 后，先运行以下检查再重新起跑。这些检查不启动 Docker、PostgreSQL 或 k6：

```bash
go test ./internal/app/
go test -tags paymenthub_benchmark ./internal/app/
python3 -B -m unittest discover -s tests/benchmark -p 'test_*.py'
```

三者分别覆盖默认构建、基准标签构建和入口逻辑。它们不验证真实负载结果，真实结果仍以实际运行证据为准。

## 怎么判断

1. 先看五轮是否都完成且具备基准资格：正式轮每轮约 24,000 笔，起发、完成与实际 HTTP 请求数核对；无丢发、超时、HTTP 或业务错误；SQL 对账一致。没起发的迭代不是服务端失败。若 k6 摘要未报告丢发，只有退出正常、目标对齐、其余关键指标完整时才接受，和显式零计数区分。
2. 保留每轮实际 RPS、P95/P99、丢发、错误和对账结果。若有失败或环境漂移，不把本次结果认作新基准，也不补跑凑齐五轮。
3. 对比小幅变化时，优先在同一天、同一台机器对照新旧版本，并核对镜像、CPU 分组和预热一致。先看可重复的变化，不从下表设置硬阈值。

## 初版实测参考

2026-11-24，运行 `3fd531a533dc`，基础源码 `5079062d4241b6496cc922ef85fe4aea894e318a` 加私有基准快照。**这是仓库固化前的实测，不是新入口的实机验证。** 运行退出码 `0`，三轮通过；未报告丢发，无超时、HTTP 或业务错误。

| 轮次 | 实际 RPS | 起发 / 完成 / 落库 | HTTP P95 | HTTP P99 |
|---|---:|---:|---:|---:|
| 1 | 200.008 | 24,001 / 24,001 / 24,001 | 16.27 ms | 42.52 ms |
| 2 | 200.000 | 24,000 / 24,000 / 24,000 | 14.33 ms | 17.03 ms |
| 3 | 200.000 | 24,000 / 24,000 / 24,000 | 23.08 ms | 35.26 ms |

三轮共完成且对账 72,001 笔，首轮多起发 1 笔按实际数量完整核对。P95 中位数 16.27 ms，范围 14.33–23.08 ms；P99 波动较大，先作观察项。原始运行证据未入库：机器汇总 SHA-256 `8eca70572402f23a2dd6b0323b7c09b2e56689c22efb99d5115aa671aa07d9ee`；执行日志 SHA-256 `10672247a565577e81ff2b41f3b2c30bdbfec19d1328df2381d04803ce167d43`。

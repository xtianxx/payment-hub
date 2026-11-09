# 开发与验证记录

**范围**：首批 T001–T004 工程准备，以及后续单独获授权的基础实现与 CI 批次；仅内部 Mock、无真实资金。业务规则仍以 [计划](../specs/001-mock-payment/plan.md)、[数据模型](../specs/001-mock-payment/data-model.md)和 [HTTP 合同](../specs/001-mock-payment/contracts/http-api.md)为准。

**记录方式**：下文第1～5节保留2026-11-06首批准备的事实与验证边界，第6节记录另行授权的远程接入。后续批次单独列明，不将历史检查结果当作新增代码已验收，任务实际状态以 `tasks.md` 为准。

本文件不是完整启动指南。首批不实现应用、SQL 迁移、镜像构建或 CI；后续只按当批授权补充。完整运行流程仍见[尚不可直接执行的指南](../specs/001-mock-payment/quickstart.md)。准备文件和静态检查不能证明支付功能可用。

## 1. 模块与版本

用户选择单一本地模块名 `payment-hub`。T001 检查时未配置 Git remote，未虚构 GitHub 地址；之后经用户单独授权建立的远程仓库见第6节。模块名仍保持 `payment-hub`，不因添加 Git remote 自动改写导入路径。

| 项目 | 批准基线 | 本批核实边界 |
|---|---|---|
| Go | `1.27.3` | 官方 linux/amd64 工具链已准备到项目专用缓存，绝对路径核实版本 |
| Gin | `v1.12.0` | 固定为应用直接依赖；真实解析结果以 `go.mod` 和本批验证记录为准 |
| pgx | `github.com/jackc/pgx/v5 v5.11.0` | 固定为应用直接依赖；显式 SQL，不引入 ORM、sqlc 或 DI 框架 |
| goose | `v3.29.0` | 独立迁移 CLI，留到 T016 的迁移工具镜像；本批不安装、不加入应用依赖 |
| PostgreSQL | `16.15` | 本批只准备 Compose 配置；未拉取或运行数据库镜像 |
| 目标平台 | `linux/amd64` | 不宣称其他平台通过 |

本机只读版本检查结果：系统 Go `1.26.5`、Docker `29.6.1`、Compose `5.3.1`、psql `16.15`、GCC `13.3.0`。不为对齐本表替换或升级宿主工具。宿主 psql 的版本不证明容器数据库已存在。

后续竞态检查需要受支持平台、启用 cgo 和可用 C 编译器；本机为 linux/amd64，已查到 `CGO_ENABLED=1` 和 GCC。这里只确认前提，未执行 `go test -race`，也未编译使用 cgo 的程序。[Go 竞态检查文档](https://go.dev/doc/articles/race_detector)

## 2. 项目专用工具链与缓存

用户已授权本批下载官方 Go 1.27.3，以及必要的批准依赖和传递依赖；没有授权镜像拉取、容器启动或系统安装。

- 官方元数据：<https://go.dev/dl/?mode=json&include=all>。
- 下载入口：<https://go.dev/dl/go1.27.3.linux-amd64.tar.gz>，实际重定向至 `https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz`。
- archive 大小：`70,588,678` 字节。
- 官方与实测 SHA256 均为 `65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40`。
- 解压前已检查成员路径；安装后的 `bin/go` 与已校验 archive 中的二进制一致。
- 实际入口：`$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/go`，未替换 `/usr/local/go`，未修改全局 PATH 或 `go env -w`。

所有本项目 Go 命令在隔离的执行环境中使用上述绝对路径。例如，下列子 shell 只核对已准备好的工具，不安装软件或下载依赖：

```sh
(
  export XDG_CONFIG_HOME="$HOME/.cache/payment-hub/config"
  export GOPATH="$HOME/.cache/payment-hub/gopath"
  export GOMODCACHE="$HOME/.cache/payment-hub/gomod"
  export GOCACHE="$HOME/.cache/payment-hub/gobuild"
  export GOENV=off GOTOOLCHAIN=local GOWORK=off GOFLAGS=
  export GOPROXY=off GOSUMDB=off
  GO="$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/go"
  "$GO" version
  "$GO" env GOVERSION GOOS GOARCH GOTOOLCHAIN GOTELEMETRY
)
```

已在该隔离配置目录执行 `go telemetry off` 并核对实际模式为 `off`。`GOTELEMETRY` 是只读查询值，单设同名环境变量不能关闭遥测。准备工具链时曾在原用户目录写入本地计数，未上传；新增的 1.27.3 计数已移除，既有 1.26.5 计数的追加无法精确回滚，已向用户说明。后续操作均隔离，不继续改写全局计数或配置。[Go 遥测说明](https://go.dev/doc/telemetry)

依赖准备时仍使用上述项目缓存、工具链和遥测配置，显式改用 `GOPROXY=https://proxy.golang.org`、`GOSUMDB=sum.golang.org`，并清空 `GOPRIVATE`、`GONOPROXY`、`GONOSUMDB`，避免继承跳过公开模块校验的设置。不使用 `@latest`，不自动切换工具链；下载失败或版本不符即停报，不静默换源、升级或关闭校验来绕过。

`go.mod` 的 `go` 声明不是实际工具链固定器；`go.sum` 是校验记录，不是独立锁文件。无应用源码时不为保留依赖创建占位包，也不运行会移除未使用直接依赖的 `go mod tidy`。模块图解析和 `go mod verify` 只证明相应模块输入与缓存检查，不代表应用编译或测试通过。[Go 模块参考](https://go.dev/ref/mod)、[工具链选择](https://go.dev/doc/toolchain)

本批真实下载后，模块图选中43个依赖模块，`go.sum` 为86行；直接依赖仍只有 Gin 和 pgx 两项。图中包含间接依赖和上游测试工具，不表示本应用运行时使用全部包。Go 的最小版本选择将 pgx 声明的 `golang.org/x/text v0.29.0` 与 Gin 要求的 `v0.34.0` 合并为 `v0.34.0`，未改变批准的直接版本。下载日志中的代理 sumdb 支持探测返回404后，正常直连 `sum.golang.org` 完成校验，不是关闭校验绕过。

## 3. 环境和配置名称

T004 的文件只包含 `postgres` 服务：基础文件 `deploy/compose.yaml`，测试覆盖文件 `deploy/compose.test.yaml`。应用和显式一次性迁移服务在 T074 补齐；不把数据库容器存活当应用就绪。

| 变量 | 用途与边界 |
|---|---|
| `COMPOSE_PROJECT_NAME` | 必填，操作者明确选择隔离项目；开发示例 `payment-hub-dev`、演示示例 `payment-hub-demo`，每次测试使用独立 `payment-hub-test-<run_id>` |
| `PAYMENT_HUB_POSTGRES_DB` | 必填，目标隔离数据库名，不提供静默默认值 |
| `PAYMENT_HUB_POSTGRES_USER` | 必填，目标初始化用户；官方镜像创建的该用户为超级用户，仅供受控本地 Mock 环境，不是生产最小权限方案 |
| `PAYMENT_HUB_POSTGRES_PASSWORD` | 必填且非空，仅从未入 Git 的本地配置注入；不记录值、不提供默认密码、不启用 trust 认证 |
| `PAYMENT_HUB_TEST_PG_PORT` | 仅测试覆盖文件必填，由操作者确认未占用端口；宿主绑定地址固定 `127.0.0.1`，不得用于发布演示 PG |

基础配置不发布 PG 宿主端口。测试必须显式同时选中两个文件和独立项目，不能把测试覆盖文件混入开发/演示操作。卷键为 `postgres_data`，不设置全局 `name` 或 `external`，由 Compose 生成 `<project>_postgres_data`；网络也按项目隔离，不设置 `container_name`。同一个项目名会复用该项目卷，不能靠更改数据库名假装隔离。外部 `-p` 可以覆盖项目名，操作前以渲染出的项目名和卷名核对目标。

容器内数据库固定 UTC；关闭全量 SQL、语句错误附带的 SQL 和绑定参数打印。这些设置不代表 PostgreSQL 诊断消息已经成为白名单应用日志，也不保证所有错误原文不含敏感信息。`pg_isready` 仅检查接收连接状态，不能证明密码、权限、迁移或业务正确。

`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD` 是 Compose 注入官方镜像的初始化变量，不是应用配置 API；已有卷不会因修改这些值自动重建数据库或轮换密码。应用的身份、API key、连接配置和测试开关在 T007/T009/T019 实现时再定名并校验，本批不创建真实演示密钥或假装已有配置加载器。

本地密钥配置使用被忽略的 `.env` 或 `.env.*` 文件，模板可使用 `.env.example`，但模板也不得含真实密钥。Compose 渲染可能输出密码；本批验证以 `--env-file /dev/null`、清理后的环境及明确标注的模拟值运行，仅归档无敏感值的断言摘要。不要把真实配置的完整 `compose config` 输出、连接串或全量环境归档。

所有项目容器使用 `local` 日志驱动，`max-size=10m`、`max-file=3`，不修改 Docker 全局配置。普通停止保留命名卷；不以 `down -v`、清库或反向迁移作为默认恢复步骤。

参考：[Compose 项目名](https://docs.docker.com/compose/how-tos/project-name/)、[静态配置检查](https://docs.docker.com/reference/cli/docker/compose/config/)、[PG 官方镜像](https://hub.docker.com/_/postgres)、[PG16 日志配置](https://www.postgresql.org/docs/16/runtime-config-logging.html)。

## 4. 基础镜像与后续 CI 引用

以下为2026-11-06公开元数据核对结果，接口均返回 HTTP 200；没有请求镜像 blob、拉取镜像、构建、运行服务或触发 CI。摘要区分多平台 index 和 linux/amd64 子 manifest，不与本地镜像 ID 混用。

| 官方镜像候选 | OCI index digest | linux/amd64 manifest digest |
|---|---|---|
| `postgres:16.15-bookworm` | `sha256:5264710be20f409750ac6dc4e5706dcef929c87e241605463f0f9aefa932a8d9` | `sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e` |
| `golang:1.27.3-bookworm` | `sha256:dc0963f32053e4bc451d24d87e3892d42d01021b61f093e98eb95c0352bb84b0` | `sha256:025eefc1e8dbe63201734a72aaa0a55d7a077f2d8410432f8ae2f3e939f32871` |
| `debian:bookworm-slim` | `sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc` | `sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018` |

来源为 Docker Hub 公共 tag 接口：

- <https://hub.docker.com/v2/repositories/library/postgres/tags/16.15-bookworm>
- <https://hub.docker.com/v2/repositories/library/golang/tags/1.27.3-bookworm>
- <https://hub.docker.com/v2/repositories/library/debian/tags/bookworm-slim>

T004 采用上述 PG 标签与 linux/amd64 manifest digest，并显式限定平台；PG16 数据挂载路径为 `/var/lib/postgresql/data`。Go 构建与 Debian 运行时只是后续 T016/T073 的候选输入，实际文件、权限、兼容性及镜像运行仍须在对应任务验证，不据此勾选镜像任务。

| 官方 Action 候选 | 稳定 release | 发布日（UTC） | 完整 commit SHA |
|---|---|---|---|
| `actions/checkout` | `v7.0.1` | `2026-07-20` | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-go` | `v7.0.0` | `2026-07-16` | `b7ad1dad31e06c5925ef5d2fc7ad053ef454303e` |
| `actions/upload-artifact` | `v7.0.2` | `2026-10-07` | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` |

分别查询 `https://api.github.com/repos/actions/<action>/releases/latest` 和 `https://api.github.com/repos/actions/<action>/git/ref/tags/<tag>`；本次三个 tag 的 ref 均直接指向 commit，不把 annotated tag 对象 SHA 当 commit。后续 workflow 按完整 commit 引用，实际 runner 要求、权限和保留上限在 T076/T077 核实；本批没有 workflow 或 CI 运行结果。

T001 时 GitHub/GHCR 的真实仓库、权限和可见性尚未确定。之后获单独授权的 GitHub 仓库与主线接入见第6节；GHCR 包关联、上传/拉取权限、镜像可见性和托管产物保留上限仍未核实，不因源码仓库已建立就自动创建包、发布镜像或扩大访问。

## 5. 本批验证与停止点

本批证据目录为 `/tmp/opencode/payment-hub-setup-batch-5yuyd1c4/`。它是本机临时准备记录，不是已完成的业务验收产物系统；不将工具存在、检查脚本或一条退出码当作支付验收。

最终焦点检查只覆盖：

1. 工具链版本、隔离缓存与实际遥测关闭；复用已校验 archive 的证据，不重复下载。
2. `go.mod` 模块身份、精确直接依赖和实际模块图，离线 `go mod verify`。
3. 用虚构路径检查忽略规则，确认源码、SQL、测试、模块文件和任务仍可跟踪；另查已跟踪敏感配置风险。
4. 用模拟配置执行 Compose 静态解析，核对开发/演示/测试的不同项目卷、端口、平台、镜像摘要、日志轮转及必填值缺失的拒绝；不连接数据库或启动容器。
5. 比对 Git 差异与本批允许文件，仅对证据齐全的 T001–T004 标记完成，T005–T082 保持未执行。

### 本批实际执行结果

最终检查入口为证据目录中的临时 `verify-setup.py`，不是尚待 T072 实现的项目检查脚本。执行 `python3 -B /tmp/opencode/payment-hub-setup-batch-5yuyd1c4/verify-setup.py`，退出码 `0`；完整 argv、白名单环境、模拟输入、输出、断言和文件 SHA256 记录在同目录 `core-verification.json`。

| 实际检查 | 命令要点与退出码 | 结论边界 |
|---|---|---|
| 工具链及隔离配置 | 绝对路径 `go env -json` 指定字段，`0`；已安装二进制 SHA256 与前次已校验 archive 的记录一致 | 实际 Go 1.27.3、linux/amd64、`GOTOOLCHAIN=local`、遥测 `off`，系统 VERSION 仍1.26.5 |
| 模块输入与图 | 绝对路径 `go mod edit -json`、`go list -m -mod=readonly all`，均 `0` | 单 module、两个批准直接版本、43个依赖模块、86行真实校验记录；检查未改模块文件 |
| 离线缓存完整性 | 绝对路径 `go mod verify`，`0`，输出 `all modules verified` | 本次 `GOPROXY=off`、`GOSUMDB=off` 仅检查已准备缓存；公开模块真实性依据为下载阶段启用 sumdb 的原始日志，不把关闭在线查询称为真实性证明 |
| Git 忽略与已跟踪文件名 | `git check-ignore --no-index --stdin` 及匹配规则输出、`git ls-files`，均 `0` | 21条应忽略、16条应保留的虚构路径全部符合；未创建夹具文件，未发现已跟踪的环境/密钥类文件名；不等于历史密钥审计 |
| Docker 忽略规则 | 读取最终 `.dockerignore` 并对照官方 `**` 语义静态复核；修订差异已留证 | 根/嵌套环境配置、日志和 Go 测试产物被排除，源码与 SQL 保留；未构建或传输实际构建上下文 |
| Compose 正例 | `docker compose --env-file /dev/null -f deploy/compose.yaml [测试时另加 -f deploy/compose.test.yaml] config --format json`，4次均 `0` | 开发、演示及两个模拟测试项目得到4个不同卷名；基础无端口，测试仅 `127.0.0.1:15432`；平台、摘要、UTC、健康检查与日志选项断言成立 |
| Compose 必填拒绝 | 同一受控环境中的 `config --quiet`，5个必填变量分别缺失/为空，10次均按预期退出 `1` | 对照报错中的变量名确认拒绝原因，不把环境错误当功能故障；未绑定实际端口 |
| 本批范围 | 基线文件 SHA256 比较、`git diff --cached --name-only`、`git diff --check` 等，均 `0` | 已有规格/模型/合同/计划/研究/清单未改，模块/配置检查后只补充本文件和任务进度；最终范围终检另存 `final-audit.json` |

Compose 检查的 `DOCKER_CONFIG` 指向临时隔离位置，`DOCKER_HOST` 指向不存在的本地 socket。四个模型中的卷名只是静态解析结果，不表示已创建卷；两个测试模型使用同一模拟端口也没有同时启动或尝试绑定。

### 准备阶段的失败与修正

- 首次 Python 公开元数据核对因 `RemoteDisconnected` 退出 `1`，未生成完整 JSON 报告；随后用独立 HTTP fetch 核对，镜像 tag 与 Actions release/ref 接口均返回200。未改用其他版本或登录账户绕过。
- 一次 `go mod download -json all` 的 shell 环境拼接错误使缓存路径无效，退出 `1`；修正为显式参数数组后，同一命令退出 `0`，43项记录无 Error。失败原始输出未单独保留，错误摘录和修正说明在 `module/module-report.json`，成功日志单独可查；不把该次失败算解析通过。
- 初版忽略规则检查误用 `git check-ignore -v` 的退出码判断负规则，曾错误报告 `.env.example` 被忽略；改用 `-q` 判断后局部47条路径通过，最终父会话37条独立路径也通过。规则本身无需为此改变。
- 静态复核发现 Docker 根目录模式不足以证明嵌套产物被排除，已改用显式 `**/` 模式，并扩大为根/嵌套 `.env*`；修订依据、差异和最终 hash 见 `ignore/dockerignore-revision.md`。这仍不是实际镜像构建验证。

首批执行时没有应用源码，因此首批不宣称应用编译、单元/功能/竞态测试通过；当时未执行镜像构建/拉取、容器启动、真实 PG、迁移、HTTP、重启、CI 或发布。后续安装、升级、额外下载或启动服务仍须按当次授权处理；缺少权限就停在相关操作，不换用真实服务或其他环境绕过。

实施前后均检查 `.specify/extensions.yml`；当前只有可选 Git 提交钩子，按本次“不提交”要求展示后跳过，不修改钩子。完成本批验证、更新任务状态并展示差异后停止，不提交、不推送，也不自动开始 T005。

## 6. 独立补充批：远程接入与准备阶段 CI

本节是 T001–T004 验收、用户批准其提交之后，另行获授权的一小批工作，不追溯扩大首批权限，也不开始 T005–T082。用户已批准建立私有 GitHub 仓库、`main` 主线、验证后提交推送并触发最小 CI，允许 runner 获取固定的官方 Actions、Go 1.27.3 和批准模块；随后明确要求使用已有 `gh` 登录创建仓库。授权不包含重新登录、创建凭据、安装或升级本机工具、发布镜像、启动业务容器或部署。

### 仓库与主线

- 仓库：<https://github.com/xtianxx/payment-hub>，已由现有 `gh` 账号创建并核实为 **Private**，未自动添加 README、License、`.gitignore` 或模板。
- `origin`：`https://github.com/xtianxx/payment-hub.git`，地址中不含凭据；不运行修改全局配置的 `gh auth setup-git`。
- 本地 `main` 已从早期宪章提交仅快进至首批已验收提交 `5bcb4f4`，没有改写历史；原 `001-mock-payment` 分支仍保留该提交，不删除或强制更新它。
- 本批首次推送 `main`，以它作为远程默认分支目标；推送后再用远端元数据和提交 SHA 核实，不把空仓库的默认分支占位值当成已有 ref。
- 后续实现继续使用功能分支和 PR；本批初始主线接入不授权自动合并后续 PR，也不自动公开仓库。

### 最小 CI 的输入与范围

工作流入口为 `.github/workflows/setup-check.yml`，可复用检查入口为 `scripts/check-setup.py`。它们只负责准备阶段，不冒充 T072 的完整 `scripts/check.sh` 或 T077 的完整业务/真实 PG/镜像检查工作流。

| 项目 | 本批约束 |
|---|---|
| 触发 | 推送 `main`，以及目标分支为 `main` 的 `pull_request`；不使用 `pull_request_target` |
| 执行环境 | GitHub-hosted `ubuntu-24.04`、linux/amd64；记录实际 Go、Docker CLI、Compose 版本，不自动安装缺少的 Compose |
| 权限 | 仅 `contents: read`；不使用业务 secrets，不申请包上传、OIDC 或部署权限 |
| 外部 Action | 第4节核实的 checkout/setup-go 完整 commit SHA；checkout 不持久化凭据，setup-go 固定 Go `1.27.3`，关闭自动追新与缓存 |
| 配置隔离 | 首个步骤先在 `RUNNER_TEMP` 内准备 XDG/Go 缓存，使用已有 Go 执行 `go telemetry off`；后续工具继承该隔离目录，不手写遥测内部格式或改全局配置 |
| 模块 | 固定工具链、模块身份及批准的直接依赖，显式下载模块后解析并校验；不允许隐式切换 Go，不用缓存命中代替校验 |
| Compose | 清理环境、`--env-file /dev/null`、模拟值；4个隔离模型及10个必填缺失/空值拒绝用例；只调用 `config`，不连接 daemon 或数据库 |
| 结果表达 | 成功只表示模块与静态配置检查通过；摘要明确列出未执行的应用和运行验收，不以“无包可测”报绿 |

Ubuntu runner 镜像会更新，平台标签不等于 Docker/Compose 的精确版本固定；实际不兼容应明确失败并诊断，不自动升级宿主或放松配置断言。

### 本地焦点检查

使用首批已核实的项目工具链和已有缓存，可离线执行：

```sh
TMPDIR=/tmp/opencode python3 -B scripts/check-setup.py \
  --go "$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/go" \
  --cache-dir "$HOME/.cache/payment-hub"
```

本机已按此入口执行，退出码 `0`：Go 1.27.3、Docker CLI 29.6.1、Compose 5.3.1；模块图为主模块加43个依赖，4个静态模型和10个必填拒绝用例均符合断言。脚本报告225条准备态断言，不是225个应用测试。本地未开启下载模式；精确命令、日志与源码 SHA256 见本批证据目录中的 `local-verification.json` 和 `local-check.*.log`。

默认离线模式只验证已经准备好的模块缓存，不声称重新取得在线真实性证明。`--download-modules` 是显式网络开关；CI 在本批已获授权的 runner 环境中使用它，通过正常 HTTPS 代理与 sumdb 下载校验。其他环境需要下载或安装时仍须按当次许可处理。

脚本只使用 Python 标准库、现有 Go 和 Docker/Compose CLI。工具缺失、版本不符、依赖校验或静态配置检查失败必须非零退出，不安装工具、不静默跳过，不打印全量环境或真实配置。模拟端口只参与渲染，未证明实际可绑定；不运行应用、SQL、镜像或容器。

### 实际执行证据与停止点

本批本机证据目录为 `/tmp/opencode/payment-hub-bootstrap-ci-k_tssfhn/`，与首批记录分开。提交前核对真实差异、既有规格/业务输入未改，以及适用的本地检查结果；推送后按本次源码 SHA 核实实际 Actions run、job/step 结果及远端 `main`，不沿用其他提交的绿灯。

远程执行发生在提交之后，因此不在被测提交中预填“CI 已通过”；实际 run URL、源码 SHA、结论与日志证据保存在本机记录并在交付时报告。遇到认证、仓库策略、额度或网络阻塞，报告实际状态，不刷新权限、启用付费服务或把未运行算成功。

完成这一小批后停止。T077 仍未完成：真实应用编译、业务测试、PostgreSQL、HTTP、并发与进程重启、配对镜像及产物留存等必须在相应实现和验证关卡补齐；本批不提供 CD、自动部署或镜像发布。

## 7. 基础实现批：T005–T011

本批获准实现共享契约、通用值约束、配置鉴权与安全日志，以及相应单元 CI。开始前已 fetch 最新远端 `main`，将本地 `main` 与 `001-mock-payment` 仅快进同步到 `72e6398978bdc8844b6edfc3b47ce67946562533`；没有改写历史。用户随后追加功能分支备份与 PR 审查许可：本批验证后提交推送 `001-mock-payment`，创建指向 `main` 的 Draft PR；不直接推 `main`、不强推、不自动合并。

### 已实现的边界

- `internal/payment/types.go`、`ports.go` 仅依赖标准库，表达平台订单、独立 Mock 事实、固定观测与处理记录，以及登记、当前接入方单笔定位、完整观测保存、Mock 付款和原交易查询的小接口。登记的 `CREATED` 必须表示本次新建且提交已确认；复用、未知或提交不明不授予付款资格。接口声明不证明事务、防重或业务流程已经实现。
- `validation.go`、`identity.go` 提供通用值校验和标准库 UUIDv4 生成。金额为含端点的 1～1,000,000 分，币种精确为 CNY；标识不裁剪、不转大小写；原因先验证 UTF-8，再按字符计数；时间按 UTC 零偏移校验。此处不实现依据接受、状态迁移或 SQL 约束，也不把平台金额/币种限制套用为 Mock 独立事实。
- `internal/app/config.go` 只从注入的 lookup 读取 `PAYMENT_HUB_CLIENT_ID`、`PAYMENT_HUB_API_KEY`，没有默认值、替代来源或自动裁剪。前者遵守 client 标识格式，后者遵守 RFC6750 Bearer 凭据字符语法；不新增最短长度、熵或 JWT 政策。无效配置返回受控错误及空配置。数据库、监听、期限、测试模式及进程启动配置后置，未创建演示密钥。
- `internal/httpapi/auth.go` 只接受唯一 Authorization Bearer 字段；头名与 scheme 不区分大小写，key 原值区分大小写。拒绝重复/合并字段和 URL、JSON、替代头凭据，不读取业务 body；只有匹配后才注入配置中的可信 client。内部请求编号与身份使用私有 context key，不采用调用者替代字段。失败统一返回 401、固定挑战、JSON/no-store 和脱敏错误，不进入后续 handler。
- `internal/app/logging.go` 的 `NewLogger(io.Writer)` 显式注入输出目标，`Log(LogRecord)` 只构造固定 JSON 白名单：UTC 时间、级别、事件、阶段、合法且已知的关联、结果与受控原因/错误类别。错误仅按已知类型或身份分类，不转发原文；未知关联省略，业务 FAILED 不自动成为系统错误。Logger 自己串行化 writer，写失败返回受控错误。未来 T022 显式接入标准输出；本批尚无真实进程或默认 Gin 日志接线。

### 测试先行与模块准备

T006–T008 测试文件先写并保存快照，再实现 T009–T011；未定义 API 时仅记录“实现未就绪”，没有把编译失败称为业务 red。鉴权与日志测试的脱敏失败消息、日志并发 writer 和一个 UUID 分隔 fixture 在实现前经审查修正，未通过放宽断言迎合实现。Gin 测试使用 `gin.New`、标准库 `httptest` 内存路由和 spy；日志测试只构造模拟 Gin/pgconn 错误，不连接数据库、执行 SQL 或监听端口。

首次离线准备缺少上游 `testify@v1.3.0` 的模块元数据，退出 1；当时 `go.mod/go.sum` 均未变化。用户随后明确允许最小补充下载。仅使用原项目工具链、隔离缓存、官方 HTTPS 代理和正常 sumdb 校验补齐固定 Gin/pgx 图，未新增直接依赖、升级版本、运行 tidy 或安装工具。实际导入图在线准备与离线只读复核均退出 0；完整缓存准备前两次遇官方端点 TLS 超时，随后同一代理/sumdb 配置下下载成功，未关闭校验或更换来源，未据此断定网络根因。并行焦点测试期间，补充下载使用临时模块输入副本，未改被测工作区；合流后由原生 Go 离线补齐校验记录，未手写 hash。最终直接依赖仍为两项，显式 indirect 20项，选中模块图含主模块共58项；图中上游工具/测试输入不等于应用运行时使用全部包。

### 可重复的本批检查

沿用第2节隔离环境和项目 Go 1.27.3，使用现有 C 编译器。以下子 shell 不修改全局配置，已准备缓存时可离线执行：

```sh
(
  export XDG_CONFIG_HOME="$HOME/.cache/payment-hub/config"
  export GOPATH="$HOME/.cache/payment-hub/gopath"
  export GOMODCACHE="$HOME/.cache/payment-hub/gomod"
  export GOCACHE="$HOME/.cache/payment-hub/gobuild"
  export GOENV=off GOTOOLCHAIN=local GOWORK=off GOFLAGS=
  export GOPROXY=off GOSUMDB=off GOPRIVATE= GONOPROXY= GONOSUMDB=
  export TMPDIR=/tmp/opencode CGO_ENABLED=1 GOMAXPROCS=2
  GO="$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/go"
  "$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/gofmt" -l \
    internal/payment internal/app internal/httpapi
  "$GO" vet -mod=readonly -p=1 \
    ./internal/payment ./internal/app ./internal/httpapi
  "$GO" test -mod=readonly -race -count=1 -p=1 -timeout=90s \
    ./internal/payment ./internal/app ./internal/httpapi
  python3 -B scripts/check-setup.py \
    --go "$GO" --cache-dir "$HOME/.cache/payment-hub"
)
```

### 本批实际结果（2026-11-07）

| 检查 | 实际结果 | 结论边界 |
|---|---|---|
| 各支线焦点测试 | payment、httpapi、app 的配置与日志选择测试均退出 `0` | 模拟值和内存路由，不证明真实进程或支付流程 |
| 项目工具链与模块 | 指定字段 `go env -json`、原生缓存准备和声明核对均 `0` | Go1.27.3、linux/amd64、cgo=1、遥测off；直接版本不变 |
| 格式与静态 | 上述 `gofmt -l` 无输出，`go vet` 退出 `0`；工作流 YAML/嵌入 bash 静态检查 `0` | 不替代业务断言或远端运行 |
| 最终合流 race | 上述三个包的 `go test -mod=readonly -race -count=1 -p=1 -timeout=90s` 退出 `0`，三个包均 `ok`，stderr 为空 | 覆盖现有基础单元测试，不是完整 MVP、SQL 或重启验收 |
| 既有准备态回归 | 上述 `scripts/check-setup.py` 离线运行退出 `0`，225条准备态断言；58项模块图、4个 Compose 静态模型及10个必填拒绝用例符合预期 | 未连接 daemon/数据库，未创建卷、绑定端口或启动容器 |
| 差异与冻结 | `git diff --check` 退出 `0`；合流检查前后源/测试与已准备模块 hash 一致，需求清单未修改 | 只勾选有实际证据的 T005–T011，不扩大后续任务授权 |

本批原始测试快照、精确命令、退出码和白名单证据位于本机临时目录 `/tmp/opencode/payment-hub-foundation-6gy5gfbr/`，不包含真实密钥或完整环境。最终入口为该目录的 `verify-foundation.py`（退出 `0`），结果与短日志见 `final-checks/result.json`、`06-race.*.log`、`07-setup.*.log`；它不是 T072 的项目全量检查脚本。日志焦点检查的第一次捕获包装脚本遇到 zsh 只读变量错误，原命令重跑退出 `0`；工具包装错误未算业务失败或通过。早期日志测试曾误用系统 gofmt，之后已用项目1.27.3重新格式化，最终版本与格式检查以项目工具链证据为准。

前后实施钩子已复核：只有可选的 Spec Kit Git 提交钩子。跳过自动钩子，按本批追加授权审查后统一提交；不修改钩子、不使用 `--no-verify` 跳过正常 Git 检查。

工作流仍为 `.github/workflows/setup-check.yml`，保留第6节的只读权限、固定 Action 引用及触发规则，新增上述三个基础包的格式、vet 和 race 单元检查。仅推送功能分支不触发此工作流，创建目标为 `main` 的 Draft PR 后才触发本批检查；必须核对本次源码和 PR 合并快照对应的 run，不沿用旧 `72e6398` 准备态绿灯。提交中不预填尚未发生的远程结果。

T012–T082 不在本批范围。数据库、迁移、跨进程夹具、业务路由、应用生命周期、真实 HTTP/重启、镜像和发布均未执行；完整 T072/T077 仍不勾选。基础单元测试通过也不代表 Phase 2 全部完成或支付 MVP 可用。

## 8. 数据库基础批：T012–T017 与持续 CI 维护

用户另行批准合并 PR #1 后推进 **T012–T017**，并允许本批所需固定镜像和 Goose 构建输入下载、迁移镜像构建、本机及 CI 的隔离测试容器，以及仅本次明确创建资源的清理。除下文用户亲自完成的一次代理配置例外，不修改宿主全局配置；不碰既有开发/演示库与卷，不改变仓库可见性、不启用付费服务、镜像发布或部署。

PR #1 已经普通 merge 到远端 `main`：`921863deae6ae76e7ea5f63b025e89d602b22f17`，两个父提交为原 main `72e6398` 与已验收功能提交 `cb35345`，合并树与原被测树一致。旧功能分支保留；新一批使用从该 main 创建的 `001-storage-foundation`，后续验证通过再推送并创建 Draft PR，不自动合并下一批。

合并后 [main CI run 42325596531](https://github.com/xtianxx/payment-hub/actions/runs/42325596531) 已独立核对通过：实际 checkout 为上述 main SHA，准备态检查225条断言及三个基础包的 vet/race 步骤成功。证据包含 run、job/step 元数据和时间窗内白名单日志，不仅是 watch 徽标；这仍只验证已合并的基础内容，不代表本批数据库测试通过。

本批先写真实 PostgreSQL 约束和迁移测试，再实现三份 SQL、独立 Goose 镜像与受限夹具。未定义 API、网络故障、镜像拉取失败或数据库不可达都不是业务 red；未完成真实迁移及 SQL 正反例前，不勾选本批完整验收。T018–T082 继续后置，完整 T072/T077 不因增量 CI 而提前完成。

### 已核实的准备与执行期故障

- 三份 integration 测试先保存 v1/v2 快照，再编写三份迁移；没有把缺实现或编译错误当成 SQL 约束 red。项目工具链离线解析真实导入图与三包 `-run '^$'` 编译门禁均退出 `0`，`go.mod/go.sum` hash 未变；此门禁没有执行数据库断言。
- Docker 代理最初不可用。用户另行允许一次有限全局例外，并亲自在真实终端修改和重启；agent 未使用管理员口令。读回有效配置后，三个批准的固定镜像 pull/inspect 成功，均为 linux/amd64；项目代码和 CI 不依赖本机代理端口，不继续修改 daemon。
- 独立 Goose 镜像实际构建成功，CLI 为 `v3.29.0`，打包的三份 SQL hash 与源码一致，非 root 且无烘焙连接信息。工具容器最初因错误的 `max-file=1` 返回 `125`；之后已使用约定的 `local/10m/3` 复验同一实际镜像 ID，旧失败及日志参数偏差证据保留，未用重建掩盖。
- 启动器源码审查修正了超时、中断、资源归属及空测试结果报绿风险；标准库 mock 回归进入 `scripts/test_check_db_foundation.py` 并由 CI 实际调用。mock 检查只验证工具自身，不代替真实 PostgreSQL。
- 首轮真实入口 run `5e813ed5cb374139821c0ba671878868` 退出 `1`：镜像检查、五项受限参数拒绝和模拟坏 DSN 的容器内安全日志检查通过；`compose up` 退出 `0`，随后 `compose ps` 因未传同一隔离插值环境退出 `1`。尚未执行迁移或 Go integration，不算 SQL 约束命中。该轮工具容器、PG 项目、网络与本次唯一卷均已清理，前后资源清单一致；保留失败报告并为环境传递补充回归，不换库或删既有数据绕过。

### 已实现的边界

- `migrations/00001`–`00003` 分别建立准确9/6/11列的三张业务表：UUID、BIGINT、TIMESTAMPTZ、TEXT/CHECK、JSONB对象、规定的唯一键、RESTRICT外键与历史索引。平台标识按原值区分大小写；Mock事实不套平台金额/币种限制，也不建跨模块外键。不加触发器、自定义枚举、付款资格字段或默认成功；依据白名单和状态处理语义仍待业务层实现。
- `deploy/Dockerfile.migrate` 使用固定基础镜像，Goose只在独立镜像构建，不进入应用module。`goose-entrypoint.sh` 仅接受默认 `up`、单个 `up/status/version/validate` 或独立CLI版本标志；拒绝危险命令及额外参数。数据库命令的原始输出在**容器内**丢弃，只保留固定JSON事件；宿主过滤不能替代这一边界。
- `tests/harness/postgres.go` 只连接启动器本次建立的数据库，公开 `OpenPostgres(t)` 与 `SeedFixtures(t, conn)`；严格校验run_id、回环显式端口、数据库名、PG16.15、UTF8及UTC会话后开启事务。夹具为三类平台订单和独立Mock事实，每个测试用有界cleanup回滚；不启动应用、不授予重付资格。
- `scripts/check-db-foundation.py` 是T017窄入口，不是T072全量检查平台。标准库mock回归由 `scripts/test_check_db_foundation.py` 保存；Compose环境传递缺陷的回归先失败、修复后21项通过，这不是数据库约束red。

### 可重复的本批入口

从仓库根运行；前提是已有项目Go1.27.3、C编译器、Python3、可用的本地Docker unix daemon及Compose。以下使用已准备模块缓存，不下载应用依赖；`--prepare-images` 显式允许缺失的固定镜像下载和迁移镜像构建，存在时仍核验实际ID、版本与SQL，不无故重建。

```sh
TMPDIR=/tmp/opencode python3 -B scripts/check-db-foundation.py \
  --go "$HOME/.cache/payment-hub/toolchains/go1.27.3/bin/go" \
  --cache-dir "$HOME/.cache/payment-hub" \
  --prepare-images --cleanup-owned-resources \
  --artifact-root "$PWD/artifacts"
```

工具/缓存缺失直接失败，不自动安装或切换工具链。需要应用依赖下载时另加 `--download-modules`，使用正常HTTPS代理和sumdb；正常vet/test仍离线、`-mod=readonly`。每次使用新的32位run_id、项目 `payment-hub-test-db-<run_id>` 和数据库 `payment_hub_test_<run_id>`，仅回环随机发布端口。依赖健康探针30秒；两次显式 `up` 的容器启动/等待各共享独立60秒期限，超时停止本次容器后失败，不后台继续迁移。

省略 `--cleanup-owned-resources` 时普通停止保留卷；提供该开关时也只删除已登记且重新核对精确名称与标签的本次卷，不执行全局prune或 `down -v`。产物 `artifacts/<run_id>/db-foundation.json` 默认保留、不入Git。脚本输出固定摘要与完整白名单报告，不归档连接值、SQL、业务整行或工具原错误。SIGINT/SIGTERM执行有界收尾；SIGKILL或宿主故障不能保证收尾，不宣称具备这类恢复能力。

当前入口执行的integration Go命令如下；T012–T017的历史执行结果另见下表，后续观察器/真实应用扩展见第9节：

```sh
go vet -mod=readonly -tags=integration -p=1 ./internal/postgres ./internal/mock ./tests/harness
go test -mod=readonly -tags=integration -race -count=1 -p=1 -timeout=120s -json ./internal/postgres ./internal/mock ./tests/harness
```

连接配置只由启动器向本次子进程注入；不要把这些命令改成连接开发/演示库的手工操作。当前最小应用可由集成夹具构建并启动，只有readyz；完整支付应用尚未实现。

### 本批实际结果（2026-11-07）

修复后的真实入口 run 为 `a5daed57a2ce449f81b19443b3d44555`，退出 `0`，安全报告与stdout中的报告一致，源码manifest前后未变。

| 检查 | 实际结果 | 结论边界 |
|---|---|---|
| 实际迁移镜像 | Goose v3.29.0、linux/amd64，三SQL清单/hash一致；复用实际镜像ID `sha256:65c3a960a3fb17370d715d52089e3113e51202f3d71592fb4101b70e808e95f1` | 本地image ID，不是已发布的registry digest；无应用镜像 |
| 安全入口 | 五项拒绝均为 `2`；模拟坏DSN为 `1`，实际容器日志符合固定事件协议 | 仅模拟拒绝回归，不是业务故障注入 |
| 实际PG迁移 | `up` / 再次 `up` 均 `0`，本机分别2.801/2.500秒；版本表1/2/3断言通过 | 不是反向迁移、恢复或业务事务验收 |
| 三integration包 | 上述vet/race均 `0`，三个包各有真实 `ok`；9/6/11列、SQLSTATE正反例、唯一/NULL/CHECK/JSON/索引/RESTRICT与夹具断言通过 | 实际会话核对PG16.15、UTC、UTF8；不包含T018+ |
| 夹具与资源清理 | 测试后直接查三表均0行；本次容器/网络/唯一卷清理成功，前后宿主资源清单一致 | 未使用或删除开发/演示数据，产物保留 |
| 默认包最终检查 | 16个Go文件gofmt无输出；`go vet -mod=readonly -p=1 ./...` 与 `go test -mod=readonly -race -count=1 -p=1 -timeout=90s ./...` 均 `0`，app/httpapi/payment三个包均 `ok` | 默认包检查不替代显式integration检查 |
| 工具与CI静态 | 21项标准库mock回归、两Python AST与shell语法均 `0`；workflow174条静态断言、7个嵌入bash语法检查通过 | 远端CI尚须对本次提交独立核实 |
| 准备态回归 | `check-setup.py` 离线退出 `0`，225条准备态断言、58项模块图；模块文件hash未变 | 不升级应用依赖，不把准备态检查冒充数据库验收 |

本机证据位于 `/tmp/opencode/payment-hub-next-foundation-e_07ygnp/`：`runtime/` 保留首轮失败，`runtime/retry-1/` 为通过轮，`final-checks/result.json` 为父会话独立证据核对与默认包最终检查；Goose构建和修订前后回归证据分开保留。真实数据库白名单报告位于上述 `artifacts/<run_id>/`。测试快照先于实现保存，没有伪造三表业务red，也未仅凭子agent声明勾选任务。

本批仅完成T012–T017，Phase 2仍未整体完成。T018–T082、完整支付/HTTP/进程重启、应用镜像、完整T072/T077和发布/CD均未实施。新分支仍需经审查提交、推送与Draft PR触发本次远端CI；本提交不预填尚未发生的远端结果，不自动合并。

### 每批必须同步审视 CI

- 保留仍适用的格式、静态和基础单元检查；新增验证层必须接入对应 PR 与 main 检查，不以旧提交、少量冒烟或无包可测代替新增代码验收。
- 本批增加独立真实 PG 迁移/约束检查；`scripts/check-setup.py` 保持准备态静态职责，不把它改称数据库验收。带 integration tag 的必测包必须被明确选中；缺工具、配置、镜像或数据库时非零退出，不静默跳过后报绿。
- 在隔离的模拟环境执行，不继承开发/演示连接；固定工具、镜像平台与构建输入，正常 HTTPS/校验，不因网络失败换版本、换真实环境或关闭验证。
- 推送功能分支用于备份，Draft PR 才触发现有 PR 门禁。复核本次 head、实际合并 checkout、job/step 与安全日志；经明确授权合并后继续核对 main 的新 run。无硬分支保护时，人工核对不能省略，也不宣称已存在服务端强制门禁。
- 扩展检查时同时检查运行预算与证据留存，遵守现有额度；额度或权限不足就停报，不自动付费、公开资源或放宽权限。输出与产物必须先脱敏，不导出连接串、原 SQL、完整环境或数据库原始错误。
- CI 与 CD 分权：普通检查保持 `contents: read`，不启用 `pull_request_target` 执行不可信代码，不上传镜像、不自动发布或部署。未来发布仍需对应任务、人工授权和已验收产物，不能因为代码增多就自动开启 CD。

## 9. 运行基础批：T018–T022（本机验收）

### PR #3 后续审查修复（本机验收）

独立审查发现旧提交 `0dcb835` 的三个反例：nil/empty显式环境实际继承宿主环境；leader回收后Stop/Wait忽略短截止且未准确记录原因；部分启动失败没有安全诊断。旧CI成功仍是旧场景事实，不能抵消反例或替代修复验证。

本次修复保持nil/empty为真正空环境；leader回收后只缩短观察/排空窗口，不向可能复用的历史PGID发终止信号，观察协程join后才发布终态。启动诊断在原绝对启动截止内预留短窗口，区分dependency/timeout/canceled，不打印底层错误或连接串，不为日志额外增加启动预算。相应测试和行为red/green证据位于 `artifacts/pr3-review-fixes/`；该目录为本机忽略产物，不等于已提交或远端长期归档。

用户进一步批准D1/D2：封口失败不应在已静止后阻断安全关池；最终归档必须使用观察器有界终结后的不可变快照。两项已完成，独立changed-risk源码复核无必修项。D1的旧行为red使用精确修复前源码overlay复现，不把它包装成原实现前的时间顺序证明。D2的旧API组合反例先复现误绿，新FinalSnapshot接口测试则在实现后验证，分别记录，不将缺方法的编译错误当red。

最终所有writer停止后，48个实际Go文件格式检查、全仓vet/race通过：207个包限定根、766个子测试，均无失败或跳过；命令包为no-test-files构建结果。45项工具回归、shell/Python与工作流7个run块静态检查通过。现有两个只读CI job未增删；默认`./...`自动纳入新增回归，原必需`TestApplicationLifecycleIntegration`内同期增加真实二进制启动超时、启动期SIGTERM取消、占用HTTP端口的诊断断言，不仅依赖任意非零退出。

fresh PG run `46181b785ac4429090ee59f5d861c061` 入口退出0，三个必需根均实际PASS。真实启动诊断三场景的执行证据来源为必需根实际PASS和无条件子测试/严格源码断言；启动器仅归档根结果，不声称另有持久化子场景JSON。默认30秒启动耗尽路径本次已实际等待并验证，其他默认写出/退出期限仍不能据此宣称全部等满。独立诊断报告临时读回后清理，外层安全报告保留。

最终安全报告`artifacts/46181b785ac4429090ee59f5d861c061/db-foundation.json`的SHA-256为`bae77e83b37bbe0821c41ed3c3a20733888e581d1b09ea819162cb35cb767777`。61项源码/模块/CI输入摘要为`0762fa70244d9ed1d1cd17bf14b761cff19daaced3068357797acff47cecc6ad`，父逐项重算与工作树一致；模块未变，三表`[0,0,0]`，本次容器/network/volume清理后另行只读查询无残留。接受记录为`artifacts/pr3-review-fixes/final-validation/parent-acceptance.json`。

限制：Seal失败后关池已动态覆盖，TestMode.Close-only失败分支仅源码核对，未另加注入seam。本节记录原功能分支修复的本机验收；本次仅本地提交，不推送，尚无修复后的远端CI结果，旧PR绿灯只证明旧提交。不自动合并或部署，US1继续暂停；D3–D6实现和P3解析重构不在本批范围。

### f4081f1 后的 CI 同步检查

核对确认新增Go回归已被默认`./...`自动执行，源码清单也已覆盖四个新测试；真实启动诊断位于既有应用必需根内，不需要新job。此次同步修正了launcher及workflow遗漏诊断场景的摘要、quickstart仍声称“不启动应用/HTTP”的过时描述，以及当前integration命令漏写`-v`的问题。

另新增1个只读配置一致性回归，比较setup检查与PG启动器共享的Go/平台/module/直接依赖/PG镜像固定值，防止只修改一处后静默漂移。当前不变量已成立，测试保存源码快照后即时通过，不伪造行为red；原45根不变，现46项工具回归通过。launcher执行AST、job/needs/触发条件/权限/actions SHA/命令/预算均未变；7个嵌入bash语法检查通过。

截至该次最小同步，P3限制仍在：integration必需结果按verbose文本名解析，未迁移为绑定Package/Test/Action的JSON，不能声称防止未来跨包同名或伪造文本；`startup_diagnostics`整体删除也还没有独立必需子测试集合防护。这些随后另获授权加固，见下节，不用摘要更新冒充门禁增强。

本轮freshPG新输入复验通过：run `f3fce5e2e5cd4f739ad1e29f5acc168a`，入口退出0、111秒，三个必需根及三包integration vet/race实际通过，三表`[0,0,0]`。安全报告SHA-256为`6b24aa8c402d6be930489280323caea7924888e69624197fb2833ed91bba03fa`；61项输入摘要`6b18b69737ccc265b522ea5dcc414b6eca9f7622955ecec0cb41b6b2f4aafc7f`由父逐项重算，本次运行资源清理后另行只读查询无残留。接受记录为`artifacts/ci-sync-f4081f1/parent-acceptance.json`。

Go业务与模块未变，复用前述207根/766子测试本机结果，不冒称又执行了默认全量。当前功能分支修复提交尚未推送，本轮CI同步改动也尚未提交，远端PR检查仍针对旧head；只有授权推送后才能核对新CI。保持两个只读job、无发布或部署/CD。

### 后续授权的六项 CI 加固（本地验收完成，升级前漏洞门禁阻断）

范围为结构化测试门禁、显式PR3回归、安全Artifact、保守并发策略、真实取消清理验证及固定漏洞扫描，不扩支付业务或开启CD。默认测试按完整Package/Test/Action核对26个固定回归根；integration另要求原三个根及`TestApplicationLifecycleIntegration/startup_diagnostics`，不将带integration标签的测试错误塞入无PG默认命令。

上传只允许重建后的白名单JSON与provenance，不能递归上传原始日志/环境或整个artifacts目录。action固定版本SHA、14天保留、禁hidden、缺文件报错；本地安全导出通过不等于远端上传已执行。并发按workflow与PR/ref分组，`cancel-in-progress: false`，暂不自动中断已有数据库检查。

取消验收只证明本次PG已healthy时，向仍运行的launcher发一次SIGTERM后，观察interrupted/sigterm、退出1及本次资源清理；不证明Go测试独立孙进程或runner硬杀后的清理。必要操作共用总预算，未确认资源状态必须失败，不能用先前快照冒充当前缺席。

漏洞扫描采用独立只读job，保留其它测试采证。固定govulncheck v1.9.0，CGO1/linux/amd64、source/symbol、integration/test配置，联网官方漏洞库但不改应用模块；JSON模式即使存在漏洞也可能退出0，门禁必须解析去重symbol findings。实际/期望Go版本、工具验证和扫描配置分别记录，不把工具错误当“无漏洞”。当前已确认报告为7个可达ID、29个不可达ID，包含测试代码的可达性不等于公网生产可利用；没有授权自动升级依赖或忽略漏洞，安全门禁保持阻断。

最终本地证据在`artifacts/ci-hardening/final-validation/verification.json`，父独立读回实际报告、核对source/export哈希和provenance，并再次只读查询两个run资源无残留（`parent-acceptance.json`）。本轮三个只读job的接线已完成；前述“两个job”仅描述此前最小同步，未开启发布或部署。

| 最终验收项（2026-11-08） | 实际结果 |
|---|---|
| 工具回归与静态检查 | 234项Python测试通过；9脚本AST、14个workflow shell块、三个只读job及固定文件上传配置通过；Go vet复用Go/module未变的本轮真实exit0 |
| 默认JSON入口 | CLI退出0，207根/766子测试、26必需根通过，7测试包通过及1个明确no-test-files包 |
| fresh正常PG | run `a4c68b228dfe45d691b5f0f1ac8c57d2` 退出0；106根/259子测试、4项integration必需结果通过；迁移两次成功、三表均0、本次资源清理通过 |
| 真实取消 | run `89a2f10d71f444b5aab831aede7fa215`；PG健康时单SIGTERM，checker退出0、launcher退出1且interrupted/sigterm；本次资源缺席、既有资源保留 |
| 固定漏洞扫描 | 实际脚本退出1，result=vulnerable，7个可达/29个不可达ID；工具JSON自身退出0不能作为无漏洞证明 |
| 安全导出 | default/db+cancellation/vulnerability分别退出0/0/1；白名单文件及provenance均生成，漏洞状态仍为vulnerable |
| 输入稳定 | 67项输入执行前后逐字节一致，digest `34d6e70b84c262849111e69a786f8198d4c46c9b19a88e98083fb6e02d2e6f9c`；Go/module未改 |

独立审查发现的重复run假绿已补计数锁存；真实执行发现Go1.27.3合法OutputType被误拒，已按官方四值枚举且仅output事件严格支持。两项均保留源码快照、真实AssertionError red与同测试hash green；初次malformed-event失败报告保留于`attempt-before-outputtype/`，没有补造绿灯。未知字段/事件仍安全失败。

该阶段结论是六项CI机制通过本地验收，升级前安全扫描正确阻断，而非整体全绿。当时没有新远端Actions运行或实际Artifact上传，也没有提交、推送、合并或升级依赖。官方修复范围见`artifacts/ci-hardening/vulnerability/public-findings-summary.md`；随后另获授权的整改见下节，旧红灯证据不改写。

### 授权依赖整改（最终最小变更，本地验收通过）

用户确认必要级联升级及固定版本下载后，仅替换六条indirect版本：quic-go v0.59.1、x/net v0.60.0、x/text v0.42.0、x/crypto v0.57.0、x/sys v0.48.0、x/sync v0.23.0。x/net要求的x/text最低版本为v0.42.0，不能强压到仅修漏洞的v0.41.0。Go仍为1.27.3，Gin v1.12.0、pgx v5.11.0不变；无replace、exclude或工具链变化。

`go.sum`仅新增上述六项及闭包x/mod v0.41.0、x/term v0.46.0、x/tools v0.49.0的18条校验记录，保留原行；MVS仍58模块，仅9项授权版本变化。历史tidy漂移曾混入11条indirect及无关sum整理，父审查拒绝后已收回；其扫描仅保留为历史，不替代最终源身份。最终download-all两次不改文件，verify与普通/integration readonly加载通过，不为整洁扩大整改。

最终证据：`artifacts/dependency-remediation/final-validation/verification.json`及`parent-acceptance.json`。最终go.mod SHA256 `cbab80e92ba5ede57c0c95b67c1899046a3622d7cbbd114d1b1109e1e5f91934`，go.sum `6ba9e14572fd680f0e28f0ead8b521f3f5501cb964a8cfa8b1cc825258b18019`。

- 重新执行Go vet、默认race JSON入口均退出0：207根/766子测试、26必需根通过。
- fresh PG run `7c89aa039bcc4149834b1004774c6354` 退出0：106根/259子测试、4项必需结果和三包vet/race通过；真实PG登录及生命周期通过，但未单独声称SCRAM-only仪表断言。迁移两次成功、三表均0，本次资源清理通过。
- 固定govulncheck v1.9.0在最终模块上实际CLI退出0，source/symbol/integration/test、CGO1/linux/amd64、Go1.27.3；可达0、不可达3（GO-2026-5932、GO-2026-6860、GO-2026-6866）。原7个可达ID均不再可达，不代表所有依赖绝对无漏洞或生产渗透验证。
- 三套本地安全导出均退出0；本轮DB只导出正常PG，不混用旧取消报告冒充完整db+cancel job。234项Python及workflow静态证据因相关源码未变复用；新模块上没有重跑取消。
- 67项输入前后稳定，digest `7fe08fd3bd89f30c6fb00861d6c73a66ec341d2ef23a1cc65bb2bd090a85aed1`；父独立核对真实红绿报告、模块和导出哈希/provenance，并再次只读查询本次Docker资源无残留。

结论仅为最终最小依赖整改的本地验证通过。旧7漏洞红灯原样保留；没有远端Actions/实际上传、提交、推送、合并或CD。取消仍仅有前轮PG健康阶段证明，不新增runner硬杀/孙进程清理保证。US1继续暂停。

### PR3 合并后 main CI 超时修复（本地验证，尚未远端复验）

PR #3 的最终 head `85c17f3` 在远端run `42482907670` 三个job及安全报告上传通过，随后用户授权合并；main合并提交为`b05da766e4a45cb74793c6b1491aecc012a3e535`。合并本身成功，但push main的run `42484196418` 数据库阶段失败：vet 77.847秒后，go test被外层150秒截止终止，stage=test/kind=timeout；取消验收因此跳过。默认与漏洞job通过，不把PR绿灯冒充main绿灯。

原失败未保留Go测试进度，无法证明当时具体哪个测试挂起。独立空GOCACHE的仅编译实验累计115.067秒；修复后空编译缓存真实集成的go test用了166.005秒并正常通过，说明旧150秒预算确实无法覆盖本地这次合法完整运行，但不把它冒称为远端故障逐事件复现。模块缓存借用既有缓存，没有下载模块或修改版本。

最小修复保留`-race -count=1 -p=1 -json`和每包`-timeout=120s`，将外层预算显式计算为冷编译余量180秒＋三个串行包各120秒＋收尾余量30秒＝570秒；不是放宽测试断言或重试。发生外层超时后，kill/reap再用既有JSON白名单解析已捕获事件，保存test_exit=124及安全进度/问题分类，仍失败，不归档原Output、stack或错误报文。

另一缺口是超时发生在源码复核前，缺失的`manifest_unchanged`被导出器当成非法字段，遮蔽了原失败报告。现合法失败报告导出`manifest_unchanged: null`、`manifest_verification: not-verified`，明确未知，不写成已变或未变；pass仍要求完整合法manifest、正确digest及明确true，非法路径/hash/类型继续拒绝。

证据位于隔离worktree的`artifacts/main-ci-timeout/`：两个修复分别保留6项和8项真实AssertionError red及同测试hash green；最终249项工具回归、9脚本AST、14个workflow shell块及原只读上传配置检查通过。真实子进程synthetic-emitter超时实验验证kill/reap、脱敏诊断及非绿导出（CLI退出1、provenance无错误），它不是Go测试。

fresh PG run `b582a69fa71e4d349682b72c779ea7c7` 入口退出0、总286.32秒；三包、4项必需结果、106根/259子测试、两次迁移和三表`[0,0,0]`通过，清理正常。首次取消报告通过但采证脚本事后丢失实际退出码，未伪造该值；另一个独立取消run `ef9ede46ef50439eb6718d8fa6ead143` 实测checker退出0、launcher退出1/interrupted/sigterm、清理通过。新取消报告与原正常PG报告联合安全导出退出0，两个身份和source/export哈希均核对。旧证据未覆盖，补验证据见`final-validation/cancellation-exit-confirmation/`。

父另行核对实际报告、哈希/provenance和Docker资源缺席。执行前后67项输入稳定，digest `c0faf6286055851e81f9ec0a30d97510c1719bf78f57fa9a53e1b0ab41c64bb8`；原US1未提交改动保持不变并暂停。Go业务及模块未改，复用当时main运行的默认/漏洞job通过证据，不重复无关扫描。该修复在当次本地验收时尚未提交、推送或重跑远端CI；不新增Go孙进程或runner硬杀清理保证，当时20分钟DB job上限不变。

### PR4 合并后默认入口超时与共享批次预算（2026-11-09，本地验收）

PR #4 head `37bc2f9` 的run `42494001384` 三个job及四份安全报告通过，父读回上传文件与provenance后，用户授权合并至main `56fc9b47b9d47317633d485dcdc58afd522e8dbf`。随后的push run `42495536802` 仍失败：此次是默认测试外层120秒超时，数据库job因needs被跳过，漏洞job通过。不能用PR4绿灯宣称main已恢复，也不能用早先DB修复替代默认入口核对。

只读锁链审查未发现`TestLifecycleFinishIncludesOutputCompletedDuringSeal`的确定性死锁；但Wait/Finish及文件I/O不是统一硬返回上界，不能仅凭deadline排除挂起。经用户批准的一次空GOCACHE诊断，同样默认race参数下实际144.563秒退出0，harness直到123.265秒才启动，目标测试0.05秒通过，207根/766子测试及26必需根完整通过。本次没有观察到hang，不声称排除了远端偶发阻塞。无测试事件区间还包括链接/启动，不能整段称为纯编译。

用户随后批准在`fix/ci-batch-budget`统一政策：现有`ci_checks.py`提供唯一`GO_TEST_BATCH_TIMEOUT_SECONDS=570`，默认与integration入口均引用。570秒是构建、链接、串行执行共用的整批监督等待限额；`180+360+30`只作容量规划，不是阶段计时器、独享额度或预留。不保证所有包各自90/120秒及Go备用强杀/输出等待时间均能叠加用满，也不包含超时后的kill/reap或报告时间。默认inner90、integrationinner120、race/count1/p1/json及26/4项必需结果不变，不拆编译/执行，不增加retry或skip。

setup job由10改20分钟，DB20、漏洞10不变。job上限覆盖整个准备/检查/取消/清理/上传，不保证各阶段最大值叠加。默认go list60、DB vet120、取消D360（preflight和报告余量仍在D外）、扫描工具准备180/扫描180均不变；本轮不新增默认vet监督器。

两类原始/安全报告新增可选`test_supervision`：`timeout_seconds`、`wait_elapsed_seconds`、`timed_out`。默认入口测量monotonic调用起点至communicate完成或超时瞬间；DB复用实际go test command的duration，均不含超时后的reap。旧报告或未执行保留unknown/null，不补造0。有限非负数、严格bool和字段白名单均校验，矛盾的timed_out不能导出通过；计时本身不授予通过资格，也不保存原事件/Output/stack。

最终证据在隔离worktree `artifacts/ci-batch-budget/final-validation/`：

- 279项Python工具回归、9脚本AST、14个shell块与20/20/10只读job配置检查通过；两个lane分别保留37项和10项实际AssertionError red及同测试hash green。旧DB预算数值本已570，不伪造其数值变化red。
- 使用正式`ci_checks.py run-default`（非诊断脚本）和新空GOCACHE，实际CLI退出0，总156.794秒；报告监督等待156.371秒/限额570、timed_out=false。207根/766子测试、26必需根、7个测试包及明确cmd无测试文件结果通过。
- 正常PG run `6c8ee7e93bee497598c67b299fa119af` 实际退出0；106根/259子测试、4项必需结果与三包、两次迁移、三表`[0,0,0]`及清理通过。监督等待62.847秒与实际命令duration一致。
- 真实取消run `e8d291861aaa43adba084ab18b1c43d1` 实际checker退出0、launcher退出1/interrupted/sigterm；本次资源缺席、既有资源保留。因未到Go测试阶段，报告不造test_supervision。默认及DB+取消安全导出均实际退出0，source/export哈希及provenance核对通过。
- 小型synthetic-emitter真实子进程1秒监督超时，退出124、reaped=true，计时不包含回收，partial解析保持非绿；它不是Go测试。
- 67项输入前后稳定，digest `8f702bebb0172b8b0ce27a22b341e877c4066a1736663c19069799fac623e632`；父独立核对实际报告/时间/哈希及另作8个Docker资源缺席查询。Go/module未变，漏洞复用main run42495536802该独立job的通过证据。

本轮真实CLI启动时的pid/starttime未持久化，只有结束后的命令/缓存扫描无匹配，故不宣称完整进程身份追踪证据闭合；容器/network/volume缺席另有查询。原US1改动保持原样并暂停。本轮仅本地验收，尚未提交、推送、合并或远端复验，不新增runner硬杀或Go孙进程清理保证。

本批已另获实施、隔离验证及新分支 Draft PR 授权；仍不合并、发布或部署。T018–T022 已分两次完成本机合流验收；远端结论须以 Draft PR 精确提交的实际检查为准，不能沿用上一批 main 绿灯。

### 观察器的可信计数合同

- `internal/testcontrol/protocol.go` 只依赖标准库，共享严格有界协议；`tests/harness/observer.go` 是父进程工具，不进入业务依赖。每条私有 Unix socket 连接只交换一帧请求和一帧响应，必须验证完整 EOF；帧含唯一结尾 LF，总长最多4096字节。未知/重复/越型字段、非法身份或额外帧均拒绝，错误不回显原文。
- 父先 `Expect` 进程代次，producer 以独立 Begin nonce 开启窗口。每次独立 Call/Reach 分配新的本地序号，即使订单相同也不能合并；只有同一事件完整同义的传输重试可去重。PAYMENT/QUERY 分计，Reach不算渠道调用。每个 run 最多64个代次、4096条唯一 Call/Reach。
- 并发事件可以乱序抵达。停止输入并排空调用/交付后，producer 以**本地分配总数 N**闭合窗口；`Seal` 核对完整 `1..N`，不能从服务器最大序号、ACK数或某次屏障回执猜N。父工具的直接 `Seal` 也必须遵守这一可信静止边界。
- 空代次集合、未 Begin、未 Seal 或存在缺口时，`Snapshot` 返回零值计数及明确不确定错误；零值不等于“精确零调用”。同号异义、第二客户端复用代次、身份/协议/交付故障以及失败的完整性断言都会锁存不确定，不能通过更换N恢复通过。最终实验结论还须在父完成子进程 Wait 后形成。
- 屏障按代次、固定控制点和确切订单匹配，一次性消费，等待不持全局锁。默认上限10秒，并受调用方更短期限约束；取消或超时使实验不确定。合法 postcommit `reply_hidden` 不是工具故障，SavingResult前置点不得冒充已提交；非法本地Release返回协议错误，不返回“已隐藏答复”。实际SQL先提交后隐藏的事实仍由T019另验。
- `StartObserver` 的ctx只约束启动；成功后由显式 `Close` 管理生命周期。只使用调用方创建的当前uid所有、0700、非symlink私有目录及0600 socket，不覆盖目标或修改全局umask。关闭只删除重验身份后的自身socket；目标被替换或清理无法确认时明确失败。中断未完成交换不能保留可信零，完全静止且已闭合的计数可以保留。

### T018 局部证据（2026-11-07）

测试快照先于实现；初始缺符号没有冒充行为red。首次运行发现socket权限/清理实现问题，保留失败后修复。源码审查补充六项生命周期回归，修复前实测5项失败、1项正例通过；随后修复屏障自身期限、已取消输入、关闭中断、清理失败及异常Accept处理。

固定项目Go1.27.3和离线缓存环境下，以下焦点命令均退出0：

```sh
go vet -mod=readonly -p=1 ./internal/testcontrol ./tests/harness
go test -mod=readonly -race -count=1 -p=1 -timeout=90s -json ./internal/testcontrol ./tests/harness
```

该源码快照包含44个根测试、288个含子测试通过，fail/skip均0；父会话核对了实际源码hash、冻结测试和执行清单。证据为本机 `/tmp/opencode/payment-hub-runtime-foundation-tnh8olst/t018-acceptance.json` 及 `t018-implementation/revision-1/` 的结构化红/绿摘要；不归档原始协议或未经处理的工具输出。

这些结果证明codec、真实私有UDS和受控生命周期机制，不证明真实SQL提交、应用重启、支付流程或CI。250/500ms的短预算测试不冒充默认10秒到期实测；观察器也不提供任意崩溃或断电下无缺口审计。T019之后的在途测试可能暂时缺少实现，以上记录不是当前工作树全量通过声明。

### T019 默认关闭接缝与真实提交证据（2026-11-07）

`Client`、app测试模式和两个消费侧Hooks沿用同一协议，不引入业务仓库占位。`LoadConfig`原身份两变量合同不变；测试模式另行解析，默认关闭且不连接观察器。启用时只接受可信私有UDS配置；消费包不导入harness或协议，nil/零值Hooks为即时no-op。

每个Client生成独立Begin nonce，按本地调用分配序号；传输最多两次同帧重试并共享操作期限，已知坏ACK/协议故障不可通过重试覆盖。`Seal`停止接入并排空在途交付；开始后新调用违反静止边界，吞错也不能让在途或缓存回执恢复成功。取消回调结束后才完成连接收尾，`Close`不隐式Seal。

隐藏答复仅允许已提交控制点：`mock.Committed`、`postgres.Registered`、`postgres.ResultCommitted`。付款/查询入口与`SavingResult`前置点不允许；工具故障不会变成渠道`FAILED`，开关不改写已有交易事实。

五份测试经过v1/v2审查并在实现前冻结，共34个根测试，其中33个默认根和1个真实PG根。父读回四份生产实现、补强取消回调join后执行四包vet/race，52个根测试全部通过（包含全部33个新默认根），fail/skip均0。另一次integration `-run '^$'`仅编译、执行0个测试，未当作SQL验收。

随后独立安全入口实际执行run `1a6b6fe2c55c4fe9b3c7aca887191a67`，退出0、耗时71秒：

- `TestCommitHookIntegrationHidesReplyAfterRealCommit`实际顶层PASS，三包integration vet/race均0；PG16.15、UTC及Goose迁移1/2/3断言通过。
- 两条独立连接不自动BEGIN；唯一UNKNOWN行真实提交后，app→postgres.Registered→UDS屏障隐藏成功答复，reader在释放前后读回事实不变，按预登记唯一ID精确清理。
- 最终三表行数为`[0,0,0]`；本次容器、network、唯一volume清理成功，父独立Docker只读查询确认无本轮资源残留。
- 报告为`artifacts/1a6b6fe2c55c4fe9b3c7aca887191a67/db-foundation.json`，SHA-256为`89913afe1ab521fc2b39c18cc90c3c703a34a8511462799c21513f94dd955d3f`。当轮43项实际源码清单由父逐项重算，运行前后一致；后续新增T020源码必须重新取证，不能沿用此清单当新树绿灯。

接受记录见本机证据根的`t019-tests/acceptance.json`、`t019-parent-validation/acceptance.json`和`t019-acceptance.json`。这些结果只证明受控提交接缝，不代表业务仓库、真实断网、支付HTTP或应用重启已实现。

### T020 真实进程与安全产物

`tests/harness/process.go`只启动显式绝对路径、显式环境的本次进程组，不经shell或继承完整环境。一个协调者管理停止原因、SIGTERM→有界Grace→SIGKILL、leader回收、输出结束和组缺席观察；强制回收及退出后的采集共用一个最多3秒截止。已观察leader退出后不再向历史group发信号；未知资源状态明确失败，不能靠Wait成功全填“已清理”。生命周期ctx、Wait/Stop取消和期限均参与收尾，默认lifetime30秒、Grace2秒；不自动延长实验期限。

`artifacts.go`在实验前写入pending报告，默认保留独立0700目录/0600文件。源码清单包含实际dirty和未跟踪输入；Finish重新核对内容，不用HEAD代替实际源码。启动预登记、记录接入、观察器绑定及封口失败均可见，`collection_complete`只表示采集完整，不自证业务成功。

stdout/stderr只做有界内存解析和白名单重编码：每行最多4096字节含LF，两stream合计65536字节。日志只接受现有12字段合同；自由`client_id/business_order_no`校验后省略。未知字段、重复键、截断、读错误或超限使采集失败；不把原始输出、环境、报文或错误落盘。测试helper显式设置`GORACE=atexit_sleep_ms=0`仅去除race运行时退出休眠，不关闭race检测；生产工具不偷偷修改调用方环境。

测试快照先于实现。源码审查补充了生命周期/封口回归，分别保留旧实现真实行为red及修后绿灯；没有把编译、夹具错误当业务red。最终风险审查还确定性复现了“先复制日志、再确认Wait”造成漏日志却报完整的窗口：两个真实helper受控交错，旧实现失败；仅后移两行快照后同一测试通过，受影响harness包75个根测试、193个含子测试完成项全部通过。失败Finish仍允许已有collector有界排空，不靠静默丢日志绕过测试。

限制：默认30秒/2秒未逐一等满；不承诺同UID恶意rename、任意setsid逃逸或崩溃下无缺口审计。静态祖先和写前后身份检查不等于通用安全文件系统。工具自身两代helper不冒充真实应用重启。

### Pass A 最终合流结果（2026-11-07）

- 默认7包`go vet -mod=readonly -p=1 ./...`及带race的`go test ... ./...`退出0；37项工具回归、225项既有setup断言、58模块离线核对、格式与工作流静态检查通过。
- 默认结果汇总器最初只按测试名索引，跨mock/postgres的四个同名根发生覆盖；原144个不同名条目保留，不能冒称完整148根原始元数据。修正为包＋测试名后，仅补测8个受影响根并记录来源。最终两行修复只重验发生变化的harness包；当前149个包限定根由66个仍有效原full-run、8个focused补测及75个修后harness结果组成，不宣称完整`./...`又重新跑过。
- fresh PG最终run为`a8bc2166a0b844b592a9ac4ed95f948c`，原安全入口退出0、80秒。两个必需根`TestCommitHookIntegrationHidesReplyAfterRealCommit`、`TestHarnessProcessPreservesParentStateIntegration`实际PASS，三包integration vet/race均0。后者在同一父Observer/数据库下运行两代真实helper，逐次Wait后独立读回已提交UNKNOWN行，计数1/1→2/2、两代回执及产物断言通过。
- 最终三表`[0,0,0]`；报告确认本次资源清理，父另行只读Docker查询确认无本轮残留。PG16.15、UTC、迁移1/2/3及再次迁移通过；没有清空既有库或降级迁移。
- 最终报告`artifacts/a8bc2166a0b844b592a9ac4ed95f948c/db-foundation.json`的SHA-256为`7ff5bd403d9e0dc8df92f6d0381dce26ec8b753ab570741e7d270ceb0503b4a6`；48项输入摘要为`084ba6347c784eb28b3dbe963e75d34b5b335aca49c643fb18b7ef6d6b0ad9b3`，父逐项重算与当前源码一致，运行前后一致，module未变。

总接受记录为本机证据根的`pass-a-acceptance.json`；统计校正和两轮风险gate见`pass-a-validation/`，修复回归见`t020-implementation/gate-fix-1/`。T020内部JSON在明确的测试临时目录中由`Cleanup`删除，未宣称长期保留；外层安全PG报告保留。仅完成T018–T020，不代表T021–T022、支付HTTP、镜像、完整MVP/T072/T077或远端CI已验收。

### Pass B 配置与验证边界

T021 的 readyz/生命周期测试与真实应用 integration 测试均先保存快照，再实现 T022。下列配置合同已落实；本机执行结果另列，不把配置表当运行证明：

| 应用变量 | 合同 |
|---|---|
| `PAYMENT_HUB_CLIENT_ID` / `PAYMENT_HUB_API_KEY` | 沿用已有身份配置，必填且不提供默认密钥；原 `LoadConfig` 两变量职责不变 |
| `PAYMENT_HUB_DATABASE_URL` | 必填完整单 host PostgreSQL URI，显式端口、库、用户、密码；当前本地 Mock 仅支持显式 `sslmode=disable`，可选单 schema 的 `search_path`（缺省 public），拒绝其他、重复参数或隐式配置文件 |
| `PAYMENT_HUB_HTTP_ADDR` | 必填显式 host:port；本次实际验证只监听 `127.0.0.1`，不开放外网端口 |
| `PAYMENT_HUB_TEST_MODE` | 未设置或 `off` 不连接观察器；`on` 仍须完整的本次私有 UDS 配置，不新增 HTTP 控制入口 |

运行配置拒绝 pgconn 识别的非空 `PG*` 设置，避免 service/passfile/SSL 文件和连接选项继承；实现不得并发修改全局环境来隔离。当前限制只适用于受控本地 Mock，不是通用 TLS 或生产数据库配置方案，不静默把 TLS 降级。

启动共享30秒包括依赖检查、迁移状态、可选测试握手和监听；不会自动迁移。退出共享20秒已包含最后2秒关闭预留，不是20加2。连接池 Close 和日志 Writer 可能无法取消，必须监督剩余期限，未确认就失败，不能宣称 Go 能强杀任意协程。容器25秒宽限仍是后续 T074 的部署合同，本批宿主进程验证不能冒充应用容器实测。

CI 已追加 `TestApplicationLifecycleIntegration` 必需根，并保留原提交钩子、父状态保留两根。第三根实际构建当前二进制，核对就绪与 SIGTERM，以及坏配置/缺迁移拒绝启动；缺迁移还核对固定的 dependency/persistence 失败事件，不能用任意非零退出自证。后续审查修复在同一必需根内加入实际启动超时、启动期取消和监听端口占用的白名单诊断，严格核对阶段/原因/错误类别与进程回收。最初门禁8项新增回归先有6项真实行为red，接线后当时45项工具回归通过；当前数量以本次工具输出为准。远端结论须以 Draft PR 精确提交的检查为准。权限、固定版本及两job保持不变，不启用CD。

首次真实导入 pgxpool 后，readonly 检查要求补齐模块记录；父用固定离线 Go 的 `go list -mod=mod ./internal/app` 原生解析，仅新增 `puddle/v2 v2.2.2` 与 `x/sync v0.19.0` 两项间接依赖。Gin、pgx、Go 直接版本不变，`go.sum` 未变；没有执行 tidy、手写 sum 或把 Goose 加进应用 module。此前仅列出 pgxpool 包的预检查虽退出0，但没有补齐实际应用导入后的记录，该前置阻断不算目标行为red。

应用初次实现的55个焦点根通过后，源码审查补齐了 context 监听、过期启动成功拒绝、退出错误保留及 Serve 完成确认。新增5个根先在旧实现上得到4项根失败，修后全部通过；不把已取消监听本来就通过的用例伪称为失败复现。

读 body 超时用例的目标仍为150ms，写期限400ms及原断言不变。Go 1.27.3 对未读完 body 的 TCP 连接有固定约500ms的 RST 避免收尾，原450ms测试清理窗口不足；旧代码曾掩盖该 Shutdown 错误。父仅将这一用例的非目标清理总窗口从500ms改为1500ms并保存独立快照，生产错误合同没有放宽，单根复验通过。原两包检查的一项失败仍保留，不能把它改写为历史全绿；最终新全量结果如下。

### Pass B 本机执行结果（2026-11-08，本机日期）

- 44个实际Go文件格式检查、全仓 `go vet -mod=readonly -p=1 ./...` 和带race的 `go test ... ./...` 均退出0。当轮实际180个包限定根、758个子测试全部通过，无根失败或跳过；7个测试包通过，`cmd/payment-hub` 为明确的 `no-test-files` 构建结果，不冒充执行过测试。汇总按包＋测试名关联，并与源码库存逐项核对。
- 最终风险审查发现取消路径会丢弃 Serve 异常，从而错误退出0；新增确定性回归先红后绿，最小保存结果且在join后分类。受影响app包48个根、162个子测试及vet复验通过，第二次风险gate接受。当前181个包限定根由133个仍有效原full-run根与48个修后app根组成，不把旧180根结果改写为新树完整重跑。
- 45项工具mock、225项setup检查、58模块离线核对及工作流静态检查通过；mock与准备态检查不替代真实数据库。
- 最终修复后fresh PG run `0e32b9e063e04e7da399eb9c306e4746` 的安全入口退出0、71秒。三个必需根均实际PASS，三包integration vet/race退出0；PG16.15、UTC、Goose v3.29.0迁移1/2/3及再次迁移通过，三表最终`[0,0,0]`。修复前`f336c53b...`报告保留为旧源码历史，不用于新树验收。
- 当前真实二进制在testmode off、没有观察器配置下，回环 `/readyz` 返回精确200/no-store，非注册路径404，SIGTERM后exit0且回收/输出/组缺席均确认；就绪未记录为付款成功。缺身份或数据库URL的子进程实际非零退出。缺迁移使用唯一空schema，固定迁移阶段诊断命中、未建对象、public三表/版本不变，精确DROP本次空schema且没有CASCADE。
- 最终报告为`artifacts/0e32b9e063e04e7da399eb9c306e4746/db-foundation.json`，SHA-256为`5b45235a5f9f2c07e72a4931afcd15eb23f29fc35968551d044b90128d881648`；57项实际源码/模块/CI输入摘要为`7ff2744ae52410a7da223b4435574ca8f37484a00c6b4d794892a9f8429e1aa3`，父逐项重算且运行前后一致。本次容器、network、volume清理均通过，父另行只读查询确认无本次残留。

总接受记录为本机证据根`pass-b-acceptance.json`；初次全量与两轮gate在`pass-b-validation/`，最终新PG及父独立复核在`pass-b-final-pg/`，定点修复在`t022-implementation/gate-fix-1/`。内部应用实验JSON读回后随明确测试临时目录删除，外层PG安全报告保留。默认30秒启动/20秒写出与退出通过常量、短期限语义及真实正常停止组合验证，未逐一等满默认时长；不是容器25秒宽限、支付HTTP、业务重启防重、应用镜像或完整MVP/T072/T077验收。不启用CD，不合并，不进入T023+；远端检查须另核对当前PR head和实际checkout，不用本段替代。

### 本批 CI 接线与证据边界

现有两个job和只读权限保持不变。默认`./...`纳入UDS、真实helper、readyz与生命周期；数据库启动器包含`cmd/**/*.go`实际输入清单，integration使用`-v`核对上述三个必需根。缺失、跳过、失败或重复结果均不能只靠包级`ok`报绿；白名单报告和摘要保存固定测试名/状态，不归档原始输出。本机工具回归45项通过，第二根与第三根接线各有原始测试快照及6项真实行为red→绿证据；这不消除首轮下述历史缺口，也不等于GitHub Actions精确当前提交已验收。

CI工具增量存在一项**流程证据缺口**：没有保存最初实现前的测试hash快照，首次red也未留原始输出，不能据文件mtime声称完整测试先行留证。之后对旧HEAD启动器与最终测试建立了明确标注的事后复现快照，实测7项行为失败、1项正例通过，再对照最终29项绿灯；这能核对回归行为，不能补造原始历史。详细记录在本机证据根的`ci-pass-a/SUPPLEMENT-process-evidence.md`，不将此缺口隐去，也不把事后复现改称原始red。

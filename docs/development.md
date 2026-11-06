# 开发准备：T001–T004

**范围**：2026-11-06 的首批工程准备，仅内部 Mock、无真实资金。业务规则仍以 [计划](../specs/001-mock-payment/plan.md)、[数据模型](../specs/001-mock-payment/data-model.md)和 [HTTP 合同](../specs/001-mock-payment/contracts/http-api.md)为准。

**状态**：T001–T004 的工程准备及本批适用的模块/静态检查已完成；其余78项未执行。本批未提交或推送。

本文件不是完整启动指南。当前批次不实现应用、SQL 迁移、镜像构建或 CI；完整运行流程仍见[尚不可直接执行的指南](../specs/001-mock-payment/quickstart.md)。准备文件和静态检查不能证明支付功能可用。

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

没有应用源码，因此本批不宣称应用编译、单元/功能/竞态测试通过；镜像构建/拉取、容器启动、真实 PG、迁移、HTTP、重启、CI 和发布均未执行。后续安装、升级、额外下载或启动服务仍须按当次授权处理；缺少权限就停在相关操作，不换用真实服务或其他环境绕过。

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

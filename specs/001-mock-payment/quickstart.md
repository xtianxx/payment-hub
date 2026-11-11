# 运行与验证指南：模拟商城一次性 Mock 支付闭环

**Go安全补丁整改已本机接受（2026-11-11）**：PR #8的标准库`GO-2026-6859`通过固定Go1.27.4最小升级消除。独立复扫可达0、未可达3；538项Python、默认55、DB24/四包、US1六/US2八/US3二十四场景、取消门禁及七类严格安全导出通过，夹具最终`[0,0,0]`且隔离资源已清理。证据见`artifacts/go-1.27.4-remediation/parent-acceptance.json`。业务依赖、go.sum、扫描阻断和预算不变；历史Go1.27.3记录保留。未提交、推送或合并，远端CI须推送后另验，不把本机通过称为远端绿灯。

**四项审查修复已本机接受（2026-11-11）**：当前结果见0.5及`artifacts/us3/review-fixes/parent-acceptance.json`。下文0.4与原接受记录保留为先前快照，不用旧run代替新增接缝验证；原审查及失败证据不删除。

**Status**: US1/US2 merged; US3 review fixes locally accepted — US3 T054–T069及最终四项审查修复的本机验证通过；可执行入口沿用0.4，当前计数及证据以0.5为准。历史证据限制、T070+、应用镜像、发布/CD及远端CI边界不变；未提交、推送或合并。

**Date**: 2026-11-11

**依据**: [plan.md](plan.md)、[spec.md](spec.md)、[data-model.md](data-model.md)、[HTTP 合同](contracts/http-api.md)。

本文不新增业务能力。0.1–0.3记录基础与US1/US2历史验收，0.4给出当前三故事合流入口；第1–7节还包含完整MVP/镜像发布的后续目标，不表示所有步骤已获验收。

所有业务与故障演示均使用模拟数据，不处理真实资金、真实支付凭据或个人信息。后续完整流程中的“应当”均为目标行为，不是本次执行结果。

## 0. 执行前的准备与检查

- 先确认本次目标是开发、测试还是演示环境，明确其独立 Compose 项目、命名卷和数据范围，不借用其他环境的数据或全局共享卷。
- 按计划核对工具链、数据库和依赖版本；本机工具存在不代表版本符合，也不授权安装或升级。正式测试、构建和 CI 使用同一已批准 Go 工具链，避免隐式工具链切换。
- 明确应用配置、演示 API key、可信接入方身份、数据库连接信息和访问地址的来源及校验要求；当前变量为`PAYMENT_HUB_CLIENT_ID`、`PAYMENT_HUB_API_KEY`、`PAYMENT_HUB_DATABASE_URL`、`PAYMENT_HUB_HTTP_ADDR`，不提供默认密钥或免鉴权兜底。不要把测试数据库URL原样转交生产入口；测试夹具会从已验证隔离目标重建最小连接配置。
- 配置、命令输出和归档都不得泄露密钥、`Authorization`、带凭据的连接串或全部环境变量。只使用已校验的关联标识，时间按计划统一 UTC。
- 应用宿主访问默认仅为 `127.0.0.1:8080`；演示数据库不发布宿主端口。测试数据库确需供宿主访问时，仅使用隔离的回环地址测试端口。
- 真实镜像部署须先取得已验证的一对应用、迁移镜像摘要，核实来源、平台与拉取权限；不得只凭标签或源码测试绿灯选用另一份产物。
- 正式业务验收或故障实验开始前，建立独立 `artifacts/<run_id>/` 采集及必要观察器，记录本次源码或镜像身份、版本、环境和预算；采集跨应用重启保持。当前入口已有白名单报告、可信观察器与真实进程夹具；观察器附件须在 Seal 后显式有界 Close，再由 Finish 消费不可变最终快照。工具两代进程实验不等于支付业务重启防重验收。

### 0.1 当前可执行：数据库与运行基础验证

已有Go1.27.4、C编译器、Python3、本地Docker unix daemon及Compose后，从仓库根运行。以下工具链/缓存路径对应本轮已核实的本机准备，不授权在其他机器自动安装；其他机器传入已准备Go的绝对路径与仓库外缓存路径。`/tmp/opencode` 为本机已存在的临时目录，其他环境可使用其受控临时目录。

```sh
TMPDIR=/tmp/opencode python3 -B scripts/check-db-foundation.py \
  --go "$HOME/.cache/payment-hub/toolchains/go1.27.4/bin/go" \
  --cache-dir "$HOME/.cache/payment-hub" \
  --prepare-images --cleanup-owned-resources \
  --artifact-root "$PWD/artifacts"
```

- 当前范围：验证固定迁移工具镜像、安全入口、隔离PG16.15、两次显式迁移、三表约束与事务夹具；integration四包还运行私有观察器、helper、订单/Mock存储及US1真实应用HTTP/重启。旧readyz-only范围及下列历史计数仅适用于当次快照；当前范围和结果见0.2。
- `--prepare-images` 明确允许缺失的固定镜像下载/构建；上述命令不下载应用模块。确需补齐模块缓存且已取得许可时另加 `--download-modules`；Goose不进入应用module。工具缺失或版本不符直接失败，不降级工具链或数据库。
- 每轮新建独立项目/测试数据库/卷，Docker分配仅回环的随机端口。只有显式清理开关且重新核对归属后才删除本次卷，不碰开发/演示资源；省略开关则普通停止保留卷，不使用 `down -v`。
- stdout提供固定安全摘要，完整报告在 `artifacts/<run_id>/db-foundation.json`，默认保留且不入Git；连接值只注入子进程，原Goose输出在容器内丢弃。日志/报告不包含凭据、原SQL或业务整行。
- 审查修复后的本机通过轮 `46181b785ac4429090ee59f5d861c061` 退出0，迁移版本1/2/3、三个必需integration根及夹具/本次资源清理断言通过；该报告只适用于其61项输入快照，不替代后续CI脚本变更的验证。较早轮的Compose配置故障属于环境失败，不算SQL约束命中。远端须另核对精确提交和实际checkout，不沿用旧run。

默认包包含环境隔离、进程截止、启动诊断、安全释放和最终归档回归。六项CI加固最终本地验收：结构化默认入口207根/766子测试、26必需根通过；fresh PG run `a4c68b228dfe45d691b5f0f1ac8c57d2` 的4项必需integration结果通过，67项输入执行前后稳定。当前入口用`go test -json`绑定完整Package/Test/Action，不能以输出文本或错包同名替代。

工具回归在CI由 `python3 -B -m unittest discover -s scripts -p 'test_*.py'` 执行，本轮234项通过；mock不代替真实数据库。真实取消验收仅证明PG健康阶段向仍运行launcher发单次SIGTERM后的本次资源清理，不证明Go孙进程或runner硬杀清理；并发仍为`cancel-in-progress: false`。

升级前固定govulncheck v1.9.0扫描发现7个可达ID并退出1，旧红灯保留于`artifacts/ci-hardening/final-validation/`。另获授权后已最小升级quic-go/x/net/x/text及必要闭包，Go1.27.3、Gin、pgx不变；最终同配置source/symbol/integration/test、CGO1扫描退出0，可达0、不可达3。测试参与不等于生产暴露，扫描通过也不保证所有依赖绝对无漏洞。

依赖整改阶段重新执行默认207根/766子测试及fresh PG run `7c89aa039bcc4149834b1004774c6354` 的4项必需结果均通过，证据在`artifacts/dependency-remediation/final-validation/`；当时US1暂停，此段为历史记录。后续main合并结果与US1恢复另见开发记录和0.2，不沿用历史报告冒充当前分支的远端job。只归档重建的白名单JSON与provenance，不上传原始日志。

### 0.2 当前已验收：US1真实提交、查询与重启

当前正常二进制提供POST `/api/v1/payments`、GET `/api/v1/payments/{payment_id}`、GET `/api/v1/payments?business_order_no=...`，三入口鉴权，`/readyz`独立。详细字段、状态和输入规则仍以[HTTP合同](contracts/http-api.md)为准。

在0.1所列工具与缓存已准备、并已授权隔离测试的前提下，可用以下入口。环境设置只作用于子shell，不更改全局Go配置；不自动安装依赖。镜像准备/本次资源清理的含义同0.1。

```sh
(
  set -eu
  cache="$HOME/.cache/payment-hub"
  go="$cache/toolchains/go1.27.4/bin/go"
  export PATH="$(dirname "$go"):$PATH"
  export GOENV=off GOTOOLCHAIN=local GOWORK=off GOFLAGS=
  export XDG_CONFIG_HOME="$cache/config" GOPATH="$cache/gopath"
  export GOMODCACHE="$cache/gomod" GOCACHE="$cache/gobuild"
  export CGO_ENABLED=1 GOMAXPROCS=2 GOPROXY=off GOSUMDB=off TMPDIR=/tmp/opencode
  mkdir -p "$PWD/artifacts"
  root="$(mktemp -d "$PWD/artifacts/stories-local.XXXXXX")"
  python3 -B scripts/check-ci-contract.py --go "$go" &&
  python3 -B scripts/ci_checks.py run-default --go "$go" --report-dir "$root/reports" &&
  python3 -B scripts/check-db-foundation.py --go "$go" --cache-dir "$cache" \
    --prepare-images --cleanup-owned-resources --artifact-root "$root/db" &&
  python3 -B scripts/ci_checks.py export-reports --report-dir "$root/reports" \
    --db-artifact-root "$root/db" --expect default,db,us1,us2
)
```

- US1合并时默认门禁为38个必需根＋R1焦点子测试，共39项；DB四包、11项。本批US2接线后的当前清单为40个根＋2个焦点子测试，共42项，DB仍选择`internal/postgres`、`internal/mock`、`tests/harness`、`tests`四包，增加至18项；这些是必需项而非全套测试总数，执行结果须以当前合流验收为准。普通`go test ./...`不执行integration标签的真实故事测试。整批监督570秒、默认包90秒、integration包120秒不变；超时后的kill/reap与报告时间不包含在570秒内。
- 模块缓存准备好后，先运行`python3 -B scripts/check-ci-contract.py --go "$HOME/.cache/payment-hub/toolchains/go1.27.4/bin/go"`；它比较普通/integration两次真实`go list`元数据，拒绝新增integration文件所在包未被数据库启动器选择，并核对清单和工作流合同，不运行数据库或下载模块。嵌套`tests/**/*.go`、`scripts/*.py`、`migrations/*.sql`自动纳入输入清单；新增必需行为仍须维护明确测试绑定，不能仅靠文件扫描。
- 工作流摘要用`check-ci-contract.py --summary default|db`从常量生成；默认摘要的`package-count`仅指必需项绑定包数，并非实际执行包总数。`CI gate`以`always()`收口三个执行job，只有全部success通过，失败/取消/跳过/缺失结果均失败。源码新增此job不等于远端已注册或成为required check：后续获授权提交/推送并完成首次运行后，仓库管理员须配置分支保护或ruleset要求`CI gate`，再核对实际阻断；本批不修改远端设置，也不启用CD。
- `tests`包自行构建正常`cmd/payment-hub`，成功/FAILED各用独立实验，经真实POST、两种GET及PG逐列检查；重启场景停止并启动同一二进制，父Observer和数据库跨代保留，核对订单事实不变、付款调用仍1次。非法请求无业务效果，另核对真实TCP的4096接受、4097拒绝、无Content-Length的chunked超限及原始连接拒绝。
- FAILED仅由可信testmode私有UDS的一次性结果指令建立，绑定实际订单/进程代次/call序号；实际Mock提交结果证明消费，未消费或Seal异常不通过。正常关闭testmode仍默认成功。业务请求不能指定失败，不使用预置FAILED行或另一fixture二进制，不支持本批未验收的“指令＋答复隐藏”组合。
- 当前启动器会运行四包全部必需integration检查，不支持故事选择参数。真实报告在`$root/db/<run_id>/us1.json`、`us2.json`及受控scenario子目录；US2内部产物使用`scenarios/us2/<scenario>`，不覆盖US1同名重启产物。上传只使用`$root/reports/upload/`中的`go-default.safe.json`、`db-foundation.safe.json`、`us1.safe.json`、`us2.safe.json`和provenance。不要上传原始报文、凭据、DSN、动态SQL或原始工具日志。失败场景的固定阶段诊断不能当PASS。
- 已独立接受的本机run为`3ed70eb46c0c48e68dc4725d706c66aa`：11/11、六场景PASS、夹具残留0/0/0、105项输入稳定、资源缺席；证据见`artifacts/us1-core/real-us1/acceptance/after-sql-evidence-fix/parent-acceptance.json`。默认38根和untagged vet复用此前已通过且输入未变的结果，不声称该轮重跑。正式PG/US1命令退出0，default/db/us1导出退出0；本轮未重复漏洞扫描或取消实验。
- CI防漂移合流后的fresh run为`8498488b3ec44d70bdf474fbce08e68a`，11/11、六US1场景及资源清理通过；同期403项Python回归、真实metadata预检、vet、默认39项与安全导出通过，107项源码清单输入稳定。父独立接受记录见`artifacts/ci-code-sync/validation/parent-acceptance.json`；此轮未重跑漏洞扫描或取消实验，不替代远端实际job。
- 之前的路径、DSN、raw响应解析及SQL证据字段适配失败均保留，不冒充产品行为red；T023最早测试先行证据缺失未补造，任务仍未勾选。US1本地运行验收不等于完整MVP、镜像或远端CI通过；当前分支已本地提交，尚未推送、发布或启用CD。

### 0.3 本机已验收：US2重放、并发与冲突验证

沿用0.2的当前合流入口与原HTTP合同，使用隔离夹具，不连接开发数据库；当前分支真实验收结果须另读本批接受记录，不能用US1历史run代替。US2增加以下检查：

- 同意图成功/失败请求顺序重放两次，返回同单及当前事实，不缓存首次HTTP答复、不再付款；FAILED仍返回原失败，首期没有重新支付或重置入口。
- UNKNOWN使用独立有效订单种子且无Mock/处理记录；两次POST仍UNKNOWN、调用/效果均不增加，不借查询核实改变该重放实验的事实。
- 空意图双HTTP竞争先在真实PG观察到本次候选事务的两个等待者，再释放；最终同单且付款入口与效果分别至多一次，允许竞争响应具有不同瞬时快照。随后同参重放与冲突请求另用独立SHARE表锁/PID绑定屏障，实际观察两者均在服务端等待后才释放，再确认200/409、两种GET结果一致及原身份、金额、币种和时间不变。报告`waiter_count`只证明初始竞争；`mixed_waiter_count`单独证明混合这对请求，各应为2，不得相互替代。
- 已有不同金额/币种返回409；可比较非法业务值的新意图400；结构/鉴权仍先拒绝。限定只读核对不登记、不付款、不写追踪；真实PG读取超时503不得冒称无订单。受控500是独立真实HTTP/实际业务层错误映射回归，不是正常应用进程真实PG内部故障的证明。
- 成功/失败均真实停止第一代、启动第二代后重放，核对同单、原结果及跨代父观察器封口；换演示密钥但保持同一client_id也不得绕过原意图，密钥不进入报告。
- 八场景`us2.json`的PASS、完整运行身份/代次凭据、命令、HTTP及PG事实缺一不可；`checks`是这些测试断言的白名单摘要，不是独立执行证明。读取超时夹具临时锁住本次fresh DB整张订单表，故仅串行执行并检查回滚，不能与其他PG任务并行。

本机接受run为`813b3f4bed304b66a4e765d7dc6e7a7c`：DB18/18、四包、US2八场景及US1六场景PASS，夹具0/0/0、117项输入稳定、本次资源缺席，default/db/us1/us2安全导出与provenance通过。父接受记录为`artifacts/us2/final-validation-after-namespace/parent-acceptance.json`。463项Python、真实metadata、untagged vet及default42沿用同批命名空间修复前的有效结果；补验仅改变两个integration标签夹具/报告测试文件，未宣称全部重跑。首轮`c008b9900fe34c95bbed1673e4aab084`因US1/US2同名重启产物目录碰撞失败，修复后同名产物并存；原失败及红绿证据保留，不算支付业务red。漏洞扫描/取消实验本批未重跑，对应实现及依赖未变。

外部审查随后确认原T050混合请求没有独立服务端重叠证明，本批补独立屏障及必需报告字段后，fresh run `7757e1e6fd034e75a5b7c0f09b778d8e`已通过18/18、四包、US1六/US2八场景和安全导出；父核对初始等待者2与混合等待者2的独立来源、混合200/409、付款和效果不增加及资源缺席，接受记录见`artifacts/us2/review-mixed-concurrency/validation/parent-acceptance.json`。同期467项Python及真实metadata通过，untagged vet/default42复用有效结果，未重跑漏洞扫描/取消实验；117项当前输入稳定。前述813b等旧run不包含新混合证明，不冒称覆盖当前字段。US2历史报告、首次失败及夹具命名空间修复证据均保留。T023历史缺证继续披露；本批已本地提交、尚未推送、尚未运行远端CI，此指南不授权US3、镜像、发布/CD或Git推送。

### 0.4 US3首次本机验收与可执行入口（历史快照；当前结果见0.5）

正常业务仍只使用既有POST及两种GET：未知时查询核实，可靠匹配才能确认；无Mock匹配不证明从未付款，不提供补付、重置或后台自动恢复。普通已确认GET不访问Mock或新增追踪。先读UNKNOWN的在途核实若遇到另一请求先确认，后来的故障/错配不能降级原确认；追踪保存失败须明确返回`TRACE_SAVE_ERROR`，不能冒称该次记录已保存。

以下是当前完整本机入口，使用已准备的固定工具链、离线缓存和子shell环境；不自动安装、下载或升级。每次使用新的`root`，遇到任何失败立即停止并检查安全报告，不盲重试。

```sh
(
set -eu
cache="$HOME/.cache/payment-hub"
go="$cache/toolchains/go1.27.4/bin/go"
export PATH="$(dirname "$go"):$PATH"
export GOENV=off GOTOOLCHAIN=local GOWORK=off GOFLAGS= GOTELEMETRY=off
export XDG_CONFIG_HOME="$cache/config" GOPATH="$cache/gopath"
export GOMODCACHE="$cache/gomod" GOCACHE="$cache/gobuild"
export CGO_ENABLED=1 GOMAXPROCS=2 GOPROXY=off GOSUMDB=off TMPDIR=/tmp/opencode
mkdir -p "$PWD/artifacts"
root="$(mktemp -d "$PWD/artifacts/us3-local.XXXXXX")"
python3 -B -m unittest discover -s scripts -p 'test_*.py' &&
python3 -B scripts/check-ci-contract.py --go "$go" &&
"$go" vet -mod=readonly -p=1 ./... &&
python3 -B scripts/ci_checks.py run-default --go "$go" --report-dir "$root/reports" &&
python3 -B scripts/check-db-foundation.py --go "$go" --cache-dir "$cache" \
  --cleanup-owned-resources --artifact-root "$root/db" &&
python3 -B scripts/check-ci-cancellation.py --go "$go" --cache-dir "$cache" \
  --artifact-root "$root/cancellation-db" --report-dir "$root/reports" &&
python3 -B scripts/ci_checks.py export-reports --report-dir "$root/reports" \
  --db-artifact-root "$root/db" --expect default,db,cancellation,us1,us2,us3
)
```

- 首次US3快照默认必需绑定53项（51根＋2个焦点），当前审查修复后为55项，见0.5；DB24项，仍为固定四包，launcher无故事选择参数。整批570秒、integration包120秒、业务10秒/Mock3秒/DB2秒/一次收尾2秒不变。镜像缺失须先取得明确准备许可，不能静默加`--prepare-images`。
- 可信固定场景只走私有UDS，US3显式`PAYMENT_HUB_TEST_CONTROL_VERSION=2`，v1默认且严格不变；版本值本身不能开启testmode。场景不接受商城字段、任意SQL或自由JSON，也不修改Mock已保存结果或`processed_at`。250ms是受控查询子期限；报告区分配置期限与含应用证明开销的实测耗时，不替代默认3秒期限测试。
- 24场景分别覆盖未知确认、故障、真实无匹配、暂不可见、四类错配、提交不明、确认保护、追踪失败、四种重启及三种生命周期；缺一失败。付款未形成交易使用隔离库真实表锁与目标事务身份；追踪失败使用精确临时trigger/function并验证清理缺席，不修改生产迁移。
- 查询报告按真实调用身份关联`queries[].request_id`、HTTP及处理记录，不按响应完成顺序拼接。追踪插入失败且确无本次记录时只报`NOT_RECORDED`，不补造决定。受控`reply_hidden`仅证明提交成功后隐藏答复路径，不等于真实网络断链；应用仅订单读回与外部订单＋同记录核对分别表达。
- T058登记后未付款、Mock提交后答复未送达两窗口使用真实应用进程中断。gen1为`controlled_truncated`、无receipt、`exact_counts=false`；真实held绑定、进程终态、独立PG事实和gen2完整Seal联合通过才接受保护，不补造跨代精确计数或全局Trusted。
- 客户端取消不等于回滚；只有目标事务终结后的PG事实才用于最终断言。应用20秒退出、宿主harness25秒宽限及清理已测；池/工作者关闭顺序部分来自既有app单测组合证据，不宣称实测goroutine数量或容器镜像行为。
- 本机接受run：`6729612a4323459984eaee49341b834c`。四包、24项DB必需检查、US1六/US2八/US3二十四场景、取消门禁与独立资源缺席均通过。修订后metadata、vet、整包编译、default53通过；最终安全消费者完整532项Python通过，六类safe JSON及provenance无错误。导出层最后只改变消费者及其测试，Go和DB/取消启动器未变，复用边界见`artifacts/us3/final-validation/parent-acceptance.json`。
- 主报告为`$root/db/<run_id>/us3.json`，内部命名空间`scenarios/us3/<scenario>`；US3报告独立上限256KiB。仅上传`$root/reports/upload/`中的六类safe JSON和provenance，不上传原始日志、报文、Authorization、DSN或动态SQL。
- 首轮失败run `9c51442cf6b4445db5f9a03a0dcfa52c`及首次导出失败保留；旧16场景PASS不追认为关联证明。Q首次完整安全动作明细缺失继续披露，后续overlay回归是新复验，不是补回首次历史；T023仍不勾选。漏洞扫描、本次应用镜像、远端CI及T070+未执行，不承诺无缺口审计、自动补付、自动补齐或生产SLA。

### 0.5 审查修复后：当前本机接受结果

- Submit与Query均识别存储返回的两个trace-save标记；仅经独立校验的原已提交确认＋空Record可直接返回。Submit新建登记的`Created=true`、HTTP 201及Location保持，复用/登记不明路径不升级201，不再次付款或新增收尾窗口；普通error附带结果仍不可信。
- `TRACE_SAVE_ERROR`的两种受控说明分别映射为固定`trace save failed`与`trace save state unconfirmed`；其他任意Issue.Message仍按code安全回退。调用方继续按code判断，不把文案变体当新增支付状态。
- 两个受控中断重启场景的gen2必须付款0次、查询1次；GET request_id必须关联实际QUERY记录。无Mock为KEEP_UNKNOWN＋MOCK_RESULT_NOT_FOUND，有匹配Mock为ACCEPT_CONFIRMATION＋完整依据。删除query/record并把计数改零的反例现已被Go及安全消费者拒绝；gen1受控截断限制不变。
- v1原始ACK含`query_directive`键一律拒绝，包括null或数字；v2合法指令、v1既有帧保持兼容。验证直接使用DecodeFrame原始字节，不以EncodeFrame的拒绝替代入站证明。
- 默认必需检查55项（53根＋2个焦点），新增`TestSubmitTraceFailureProtection`及`TestTraceSaveIssueVariants`，旧53项完整保留；DB仍24项/四包。533项完整Python、真实metadata、vet、default55及integration编译通过。
- fresh PG run `932b50b41ea1450385051fad4b2f0a94`四包/24项必需检查、US1六/US2八/US3二十四场景通过；新增Submit真实PG服务组合子例在既有ObservationFaultIntegration根内执行。该子例不等于正常应用二进制HTTP端到端；201/Location/双issue及消息安全另由实际路由JSON测试证明。
- 先前补验run `7abfb873d1a84104a1a288ac992d6e5b`因新增夹具漏清真实Mock行而最终失败，`[0,1,0]`证据保留。只补本次order ID精确清理及零行核验后重新执行，当前最终`[0,0,0]`；不放宽残留门禁。该修订仅影响integration夹具，未受影响的Python/default/metadata证据按记录复用，没有冒称全部重跑。
- 当前取消门禁、六类严格安全导出及provenance通过，正式与取消窗口串行且资源独立确认缺席。新接受记录为`artifacts/us3/review-fixes/parent-acceptance.json`，原`artifacts/us3/final-validation/parent-acceptance.json`保持历史原样。未运行远端CI、漏洞重扫、镜像或T070+；不承诺无缺口审计、自动补付、生产SLA。

## 1. 启动数据库

**目标操作**：确认项目与数据卷后启动隔离的 PostgreSQL，等待数据库就绪。

**完成判断**：目标数据库实际可连接，且连接的是预期环境；容器正在运行不等于数据库已就绪。应用后续仍须自行检查必要依赖，不依赖启动顺序保证整个生命周期可用。

**失败边界**：无法确认环境、卷或连接目标时停止；不能清空数据库、删除命名卷或切用其他环境来绕过启动故障。

## 2. 显式执行独立迁移

**目标操作**：使用配对的迁移工具及 SQL，明确触发一次性 goose 迁移任务，检查退出结果与目标数据库的迁移状态。

**完成判断**：迁移任务成功结束，实际数据库达到应用要求的迁移状态；只看到工具进程启动不算成功。

**失败边界**：失败或执行结果不明时停止并核对事实，不继续启动应用，不默认回滚、反向迁移、清库或删卷。迁移任务使用计划规定的独立期限，不套用短业务事务期限。

## 3. 启动应用并确认就绪

**目标操作**：在配置、数据库和迁移状态符合要求后启动应用，检查 `GET /readyz`。应用不偷偷执行迁移，普通重启不续跑付款。

**完成判断**：运行检查按计划返回就绪状态，且未暴露配置或业务信息；它无需 API key，不读取业务订单、不调用 Mock，也不能证明所有支付功能正确。

**失败边界**：配置缺失或无效直接失败，不退化成匿名访问。依赖未就绪时不开始业务演示；应用未监听或已退出可能没有 HTTP 响应，不能据此宣称就绪。

正常演示关闭测试控制能力，不依赖测试父进程或观察器。测试能力只由可信准备过程启用，使用私有 Unix domain socket，不开放商城控制入口，也不向业务容器挂载 Docker socket。

## 4. 通过真实 HTTP 入口演示业务

请求和响应的唯一详细定义以 [HTTP 合同](contracts/http-api.md) 为准；本指南不复制另一套字段、错误码或媒体类型规则。

| 操作 | 必须观察的事实 |
|------|----------------|
| 提交一个有效的新支付意图 | 取得平台订单身份，分别检查付款结果与本次处理问题；新建不等于付款成功 |
| 分别按平台号和原业务订单号查询 | 在当前接入方范围定位同一笔订单，不误用为列表或其他身份查询 |
| 重放相同支付意图与参数 | 复用原订单，不改原始事实，不产生第二次付款；不要求永久冻结第一次响应 |
| 重放相同意图但金额或币种冲突 | 按合同拒绝，不覆盖订单、不触发新付款 |
| 查询未知结果 | 核实原 Mock 交易，匹配依据才确认；仍无法确认时保留未知，不猜成失败或换号重付 |
| 查询已有确认结果 | 查询或额外追踪故障不降级确认事实；问题仍须如实表达 |

- `2xx`、订单结果和处理问题分别判断，不能用 HTTP 成功替代付款成功或追踪完整。
- 成功、明确失败和未知的受控场景由可信测试准备建立；不向商城业务请求添加强制成功、失败、查询故障等控制字段。
- 明确失败后不支持再次付款；答复丢失、超时、重启或 Mock 无匹配记录也不授予重新付款资格。

## 5. 分层执行验收并留证

按计划为17个业务验收场景及新增 HTTP 边界安排主证据，不要求每项在所有层重复运行：

| 验证范围 | 证据用途与限制 |
|----------|----------------|
| 业务规则 | 验证金额、冲突、结果接受及保护分支，不能替代数据库并发与事务事实 |
| 真实 PostgreSQL | 验证唯一键、短事务、行锁、新确认与最小依据原子保存及故障核对 |
| HTTP 集成 | 验证实际路由、鉴权、严格输入、响应及存储效果；流式大小限制覆盖真实连接 |
| 真实进程重启 | 宿主机测试父进程跨应用子进程重启保持，数据库保留，核对三类订单和不重付 |
| 实际镜像验证 | 单独验证配对镜像的迁移、启动、关键闭环与保留数据的重启；不能用宿主二进制结果替代 |

- 付款调用在 Mock 防重前独立观测，交易事实或效果另查，不能用一条交易记录证明只调用了一次。查询与付款分开计数，观察器不能替业务防重。
- 只有受控窗口和前后证据完整时才报告精确次数；观测缺口判不确定或失败，不能按零调用通过。重新创建 Go 对象不算真实应用重启。
- 普通超时测试使用演示默认期限；故障屏障实验按计划同步调整相关上层预算并记录差异，不能用放宽后的结果证明默认时限或性能。
- 隐藏真实提交的成功答复可验证受控提交不明路径，但不等于验证真实网络断链；任何取消、断连或强退都不直接证明事务回滚。
- 实际记录场景、脱敏命令、退出码、未执行项及原因、版本与预算、必要响应、白名单数据库状态、调用观测、进程代次和脱敏日志，不以截图、单个退出码或 AI 声明代替断言。

## 6. 保存证据后停止，保留数据库

- 确认采集结束并完整保存，不能仅依赖可能被轮转或随容器移除丢失的日志。采集失败、无法检索或文件不完整时，明确报告证据不足，不宣称留痕验收通过。
- 按计划执行有总期限的退出：先不就绪并停止新请求，等待在途工作与有限收尾，数据库使用者退出后关闭连接池，再处理剩余资源；不新增付款或无限延长收尾。
- 普通停止、重启及替换应用均保留命名卷，`down -v` 不作为默认步骤；清理仅针对明确确认的隔离目标，不顺便删除开发或演示数据。
- 本地验收产物默认不自动删除。CI 产物另受计划中的保留目标与平台上限约束，不能当作永久档案。

## 7. 发布或恢复失败时停止诊断

镜像发布流程以 [plan.md](plan.md) 为准；本指南不创建包、触发工作流、配置凭据或部署。

- 检查或镜像验收失败不发布；推送后拉回复核失败，也不能把候选标记为可用。
- 操作者只选已验证的一对摘要，不在部署时重新构建；迁移或启动失败停止，不自动降级迁移、清库或删卷。
- 切回旧应用前先确认当前数据库兼容，换镜像不代表数据库已经回退。未知付款仍按原意图查询核实，不补付。
- 追踪、日志或采集故障如实报告，不改写已确认付款结果；客户端断开时不承诺错误响应已经送达。

## 指南完成状态与后续补齐

| 状态 | 判定依据 | 当前情况 |
|------|----------|----------|
| 设计写清楚 | 操作顺序、检查条件与失败边界已记录并评审 | 已批准设计；0.4给入口、0.5给当前审查修复结论，历史记录保留 |
| 命令实际执行成功 | 对应实现存在，有实际命令、退出码及证据 | 三故事宿主正常二进制/真实PG/HTTP/重启及取消、安全导出通过；镜像未验收 |
| 业务验收通过 | 关键业务断言成立且证据完整，未执行项已披露 | US1/US2/US3本机故事范围接受；历史证据限制保留，完整MVP未验收 |

后续T070+、配对应用镜像及发布流程须另获授权、分别实施验收。不得把三故事本机绿灯当作完整MVP、当前分支远端CI或部署产物已通过。

本文件不授权安装、启动、迁移、构建、测试、发布、清理数据、创建外部资源、Git 提交或推送，也不授权进入 tasks 或 implement；实际执行仍需相应授权。

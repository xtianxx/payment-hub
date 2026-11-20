# 本地镜像与受控发布

> 本分支工作流入口调整为`ci.yml`、`nightly.yml`、`release.yml`，
> CI构建验收一次，CD发布只消费同SHA可信CI候选，不重新构建镜像。
> 来源绑定、四文件交接及本轮未实现的nightly实验见[CI/CD说明](ci-cd.md)。
> 下方历史运行保留原文件名和证据，不代表旧入口仍用于当前发布。

## 正常发布策略：仅更新已存在的public包（2026-11-15）

负责人已确认暂停工作流自动创建缺失包。无论源码仓库公开或私有，
两个目标包都必须已存在、为public、属于`xtianxx`并正确关联
`xtianxx/payment-hub`；否则在准备发布输入、Docker load/login/tag/push前停止。
缺失分别报告`package/app-absent`或`package/migrate-absent`。

工作流不再提供`bootstrap_packages`输入；底层仍保留`--bootstrap-packages`参数和摘要的
`bootstrap_requested`以兼容已有脚本调用，但它们不授权创建包。
设为true且任一目标缺失时仍失败；两包已满足要求时只执行同样的正常更新。
缺失判断、空清单`last=0`兼容和只读审计保留，不能将空清单解释为创建许可。
负责人已明确将两包的目标可见性从private改为public；private和internal包不满足当前策略。
此具名策略修订同时记录于[Git协作规范](git-workflow.md#5-发布标签与回退)，
不构成公开凭据、敏感数据或任意其他软件包的授权。
源码仓库可见性仍独立，不以`repository.private`的值替代包自身可见性检查。
public包可匿名读取且不能直接改回private，公开包内容不应包含任何凭据或敏感数据。

缺失迁移包使用独立、默认关闭的`initialize-migrate`操作，不借用bootstrap开关，
不改变上述正常发布门禁。首次推送可能直接public，必须独立确认风险；
实际为private时也不自动更改可见性。入口、权限、互斥确认和恢复边界见
[迁移包一次性初始化](migrate-initialization.md)。
本轮只实现和验证代码，不执行初始化或正式发布。

registry验证允许匿名GET直接返回200，但必须完成与鉴权响应相同的manifest大小、
内容哈希、响应摘要、媒体类型、根描述及runtime/attestation校验；
匿名200不等于跳过镜像身份验证。若仍返回401，则继续使用原受限challenge、
writer Basic及scoped Bearer路径，审计token仍不能用于registry认证。

本轮public策略本地验证：`python3 -B -m unittest discover -s scripts -p 'test_*.py'`
通过1093项测试；固定Go1.27.4的CI合同检查、`TestReleaseGates`和差异检查均退出0。
匿名200与401认证路径共用完整身份验证的正反例均通过；
这不代表已执行真实发布或独立确认两包远端状态。

### 本次失败证据及处置边界

运行[`43677641633`](https://github.com/xtianxx/payment-hub/actions/runs/43677641633)
（源码`fbdab728aa9e16485ac50077c52970bab51b4230`）的prepare通过，
两包初查缺失；app push正常返回后，包元数据可见性校验报`package/app-visibility`，
migrate未尝试。摘要`push_attempted_roles=["app"]`、
`push_side_effects.app="unknown"`及空`pushed_roles`不代表没有远端写入。
后续匿名读取该次app源码标签manifest返回HTTP 200，证明它当时可公开读取。
这不是“没有上传成功”的无副作用失败。

[Actions创建包的默认规则](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions#default-permissions-and-access-settings-for-packages-modified-through-workflows)
说明，工作流使用`GITHUB_TOKEN`创建包时默认继承仓库的可见性和权限模型。
此前仅依赖通用首次默认private及普通仓库关联规则的判断不完整。
该规则与本次公开仓库首次创建的执行路径及公开可读结果一致，
但缺少包设置变更审计，不能据此声称已经排除外部操作。
当前策略不再以任何首次默认可见性假设批准创建。

此前normal-only修复只修改前置门禁、回归和说明，没有删除或重建现有public包。
本轮可见性策略调整不改变源码仓库可见性、不扩大审计token权限、不新增写入PAT，
也不授权创建缺失包、初始化、发布或部署。public包不能直接改回private；
保留原名删除重建、更名及独立初始化均须另行明确授权和验证，
不能靠重跑工作流试探。初始化必须使用真实已验收的配对镜像，
不可用占位镜像冒充。此前未实现该恢复路径；本轮新增独立初始化入口，
但代码实现授权不等于真实首次推送、包创建、公开或正式发布授权。

本门禁只阻止已检测到的缺失包进入push。完整分页和push前后重查不是原子锁，
外部写者仍可能在最后检查后删除、重建或改变包可见性；
不能宣称normal-only完全消除了创建竞态或首次暴露风险。
出现部分成功或未知副作用时继续停止并人工核对，不自动重试、删除或回滚。
本节覆盖下文历史初始化授权和首次创建步骤；当前不存在可用的自动创建模式。

此前private目标下的normal-only本地验证：`python3 -B -m unittest discover -s scripts -p 'test_*.py'`
通过1088项测试；固定Go1.27.4执行的CI合同检查、
`go test -mod=readonly -run '^TestReleaseGates$' -count=1 -p=1 -timeout=90s ./tests`
和差异检查均退出0。回归覆盖兼容开关开/关、两包全缺失/仅一包缺失、
空清单`last=0`、首次Docker操作前拒绝，以及两包有效时的正常更新。
这些是历史离线/fake验证，不能代替本轮public策略验证；未据此重跑真实发布，也未验证任何新的初始化方式。

### 现有包可见性操作

仅对已存在的目标包执行本次已授权的private到public调整，不因包缺失自动创建。
登录GitHub后，在owner的Packages列表进入目标包的Package settings，
选择Danger Zone中的Change visibility，再选Public并确认。
如果包已是public，无需重复改动；如果列表中不存在，不创建、不推送试探。
匿名页面404不能单独证明包不存在；缺少管理权限时必须明确保留“未核实/未修改”状态。

本轮只读核查中，app公开页面显示Public，未重复更改其设置。
owner确认migrate原本为Public，但独立匿名页面返回404、
匿名registry token请求返回403，尚未独立验证其公开读取状态；
这些响应不能单独区分缺失、私有、名称不符或访问限制。
本地CLI缺少`read:packages`且没有可用的已登录管理浏览器，因此本轮未写入任何远端包设置。
恢复发布前仍需核对准确包名、public属性及仓库关联，不能用本地测试代替。

### 公开镜像对的消费门禁

两个GHCR仓库的push不是原子事务。app推送成功而migrate或后续验证失败时，
app标签仍可能已经被匿名读取；失败不会隐藏、撤销或自动删除已公开内容。
源码标签、单个包页面、一个digest可拉取或某次push成功，
都不是“完整镜像对已发布”的承诺，不得据此部署或更新。

消费者（包括人工部署操作者和后续自动化）必须先满足以下全部条件：

1. 从受信仓库取得对应发布run及attempt的`release-summary.json`，
   核对该run的源码SHA和预期交付SHA一致；不拼接不同run、attempt或源码的两份结果。
   发布工作流必须全部结束且结论为success；仅prepare、PR CI或单个job成功不够。
2. 摘要必须为`payment-hub.release-summary.v1`、`stage=publish`、
   `outcome=pass`、`published=true`、`failure=null`，
   且`credentials_removed=true`、`cleanup_failed=false`。
   缺失、未知、矛盾或任何失败状态均拒绝消费，不默认成功。
3. `descriptor_sha256`必须匹配该run受信交接值，descriptor源码SHA也须一致。
   `pushed_roles`必须包含且仅包含app和migrate，二者的`push_side_effects`均为`verified`；
   两role的`push.exit`均为0，且各自`push.candidate_digest`、`remote.digest`
   与descriptor中的对应`root_descriptor.digest`一致；
   两role的`pull.exit`均为0、`pull.id`与descriptor中的对应`local_id`一致。
   两包`packages_postcheck`元数据必须完整且符合public、owner、仓库及原ID要求，
   `revalidation.result`必须为`pass`。
   单role完成或`unknown`副作用均不是可用发布对；
   即使两项都为`verified`，也不能跳过pull、重新验收及整体成功判断。
4. 使用该摘要中经验证、属于相同源码及交接描述的两个registry digest固定镜像引用，
   不用可移动tag代替digest，也不与历史版本的另一个角色混配。
   公开下载权限本身不代表这些镜像已获验收或部署授权。

目前没有自动阻止匿名用户下载半成品标签的机制，任意外部读者不受文档技术约束。
本消费门禁定义受控消费者何时可将镜像对视为完成，不声称在两包验证前隐藏镜像，
也不新增自动部署或原子发布服务。
不满足上述条件时只能保留诊断和部分副作用记录，等待负责人另行处置。

#### 发布消费验收工具

正常发布成功后，同一 `release-summary-<run_id>-<attempt>` Actions artifact
保留原有 `release-summary.json`，并归档原始 `descriptor.json` 和
`release-evidence.json`。交接记录绑定发布 run/attempt、预期源码、源 CI 候选及其
descriptor 摘要；摘要记录实际核验到的包 owner。诊断用的失败摘要不是消费凭证，
初始化也不会生成这份配对凭证。历史运行若缺少这些证据，不能自动视作通过。

`scripts/verify-release-consumable.py` 只有在四条消费判据均成立时才输出两条
`镜像名@sha256:…` 引用。可提供独立预期的 app 和 migrate digest，必须成对提供，
并且分别匹配自己的角色。离线模式只证明输入文件彼此一致，不能证明它们来自
GitHub，也不能把本地自填的运行元数据当成发布凭证。

在线取证须独立提供预期源码 SHA、发布 run ID 和 attempt，不从摘要反推预期值。
先从独立来源填写下列变量；示例使用带引号变量，避免 Bash 把 `<…>` 当作重定向：

```bash
SOURCE_SHA="<40位源码SHA>"
RELEASE_RUN_ID="<发布run-id>"
RELEASE_RUN_ATTEMPT="<attempt>"

python3 -B scripts/verify-release-consumable.py --online \
  --expected-source-sha "$SOURCE_SHA" \
  --release-run-id "$RELEASE_RUN_ID" \
  --release-run-attempt "$RELEASE_RUN_ATTEMPT"
```

凭据仅从 `GH_TOKEN`（优先）或 `GITHUB_TOKEN` 环境变量读取，不放进命令行。
凭据需要能只读访问该仓库的 Actions 运行和 artifact；
本工具不需要 `write:packages`，也不使用发布专用的包审计凭据。
在线模式按指定 attempt 的完整 artifact 名选择唯一 ID，验证下载 ZIP 的 API 摘要，
再核对该发布及其交接记录指定的源 CI run/attempt 和 bundle artifact 身份。
缺失、过期、重名、摘要或来源不符均失败，不回退到旧 attempt 或其他候选。
签名下载地址不输出；存储端请求不携带 GitHub bearer token。
在线取证在支持 `SIGALRM` 的主线程中运行，整次操作有 90 秒 Python 层时限；
阻塞在原生调用（例如系统域名解析）时，信号处理要等该调用返回，实际退出
可能超过 90 秒。不支持该机制的平台或线程会直接拒绝，不降级为仅有 socket
空闲超时。
若调用进程已有活动的 `ITIMER_REAL` 计时器，或主线程屏蔽了 `SIGALRM`，
在线接口同样拒绝执行，不接管、清除或解除调用方的信号状态。无法通过
Python 恢复的 `SIGALRM` handler（`getsignal` 返回 `None`）也会被拒绝。
同一进程内并发的在线验收会被拒绝。外部组件仍不应与验收过程同时修改
进程级 `SIGALRM` 状态；工具只能尽力避免覆盖已经检测到的 handler 和计时器。
在线验收不要求发布或 CI workflow 当前处于启用状态，但路径不符、已删除或
无法读取仍会拒绝。
签名地址仅接受 HTTPS 的 GitHubusercontent 或 Azure Blob 固定主机域；
若 GitHub 更换存储域名，工具会拒绝，需要先审查并更新允许列表。

离线复核使用四份本地文件，其中运行元数据应是指定 attempt API 的原始 JSON 对象：

```bash
python3 -B scripts/verify-release-consumable.py \
  --summary release-summary.json --descriptor descriptor.json \
  --evidence release-evidence.json --run-metadata run-attempt.json \
  --expected-source-sha "$SOURCE_SHA" \
  --release-run-id "$RELEASE_RUN_ID" \
  --release-run-attempt "$RELEASE_RUN_ATTEMPT"
```

两种模式都可追加 `--expected-app-digest sha256:<64位摘要>` 和
`--expected-migrate-digest sha256:<64位摘要>`；必须成对，且不能交换角色。
成功输出 `mode=online` 或 `mode=offline` 以及 `app=`、`migrate=` 固定引用；
失败非零退出，仅输出脱敏分类，不输出部分镜像对。
`mode=online` 表示完成 GitHub 来源核验，不是独立签名或实时 GHCR 探测；
`mode=offline` 仅表示输入一致。
证据包不含 `pair.tar`，消费者不重新计算其归档字节摘要，也不重新执行镜像验收。
工具信任仓库及 GitHub Actions 运行与 artifact 的权限边界，不防御拥有相应权限者
对整套工作流和证据的恶意替换。

CI 只执行验证器的离线回归，不要求 PR 或 `main` 已经发布，不新增分支保护门禁。
真正消费时，凭证通过后仍须以输出的 digest 拉取同一对镜像；凭证不是实时
GHCR 可用性检测，不触发拉取、数据库迁移或部署，也不授予这些动作的权限。

### 暂停创建策略的上线与发布检查清单

以下是操作门禁，不是新增的平台锁或自动取消功能。合并只影响后续使用新提交的运行，
不能替换已启动任务检出的旧代码；重跑历史任务也不能假定会加载最新main。

**合并前：**

1. 由owner安排维护窗口，停止新增手动发布及历史发布重跑；确认没有其他仓库、
   本地客户端或外部自动化正在写入两个目标包。
2. 在Actions中检查`release.yml`及历史`publish.yml`的全部未结束运行，包括排队、执行中、等待审批
   或待启动状态。逐一记录run ID、attempt、源码SHA、状态及核查时间；
   不能仅按分支名或最近一次运行判断，也不能把取消请求已受理当作已经停止。
3. 如果存在仍使用旧创建逻辑的运行，暂停合并；获得明确取消授权后取消，
   等待其所有作业终止，再检查摘要和实际包状态。即使显示cancelled，
   也不能推断已发起的push被撤销。未能确认停止或远端副作用时继续保持暂停。
4. 按Git协作规范重新核对PR head、最新main基线、必需检查和审查讨论。
   “当前没有活动运行”只是当时快照，不能替代紧邻合并动作的再次核查；
   维护窗口必须覆盖核查、合并及新策略生效的过程。

**合并后及恢复发布前：**

1. 等待合并提交对应main检查通过；不自动恢复发布。
2. 禁止通过重跑旧run、旧job或选择旧源码SHA恢复发布。后续确有发布授权时，
   从已包含本门禁的当前main新建运行，核对该run的实际源码SHA，
   不复用旧运行的bundle或旧源码候选。
3. 两包必须事先均有效且public；仅有app公开不代表migrate已存在或符合要求。
   任一包状态未核实、缺失或元数据不符时保持停止；
   删除、重建、更名及独立初始化不包含在本检查清单的授权中。
4. 从最后一次包状态检查直到发布结束，owner及其他有权限的操作者不得删除、
   替换目标包或更改可见性、关联和访问权限；存在外部写者或无法保证维护窗口时，
   不开始发布。

上述纪律只能降低并发风险，不能把检查和push变为原子操作。
若要求技术上绝不创建包或绝不发生首次公开，现有normal-only门禁不满足这一保证，
必须先批准并验证更强的写入权限边界或协调方案，不能增加一次GET就宣称风险消失。

## 包清单分页失败诊断（2026-11-14）

真实发布运行`43599112781`、upload job `132462192508`在源码
`3826d42167f09e783b7caeb57587c391567e132c`上报
`package/inventory-link`。该运行的日志和摘要未记录实际Link头，
因此不能确定是哪项导航校验失败，也不能据此认定某种URL格式就是根因。
该次摘要的`push_attempted_roles`、`pushed_roles`均为空，
`published=false`；按代码执行顺序，此次失败早于login/push。
这不代表其他运行或重跑也没有副作用。

本轮补固定、脱敏的分页拒绝分类：内部异常和错误日志保留细分类，
摘要则保留原有`failure.stage=package`、`failure.kind=inventory-link`，
以新增的可选`failure.detail`记录固定拒绝原因。
不输出原始Link、URL、查询参数值、包清单或认证信息。
这只是可观测性改进，不放宽路径、同源、查询参数、页号或完整分页门禁，
也不把畸形或未知响应当作缺失包证据。

旧消费者仍可精确匹配`package/inventory-link`，并忽略未知字段；
需要具体原因的消费者再读取`failure.detail`。仅当内部错误属于以下
固定白名单时才映射，不按任意字符串前缀归类。
其他错误保留原有`stage/kind`，不增加`detail`；成功摘要不增加错误详情。

例如查询参数校验失败的摘要为：

```json
{"stage":"package","kind":"inventory-link","detail":"inventory-link-query"}
```

| detail（亦为内部异常及日志的细kind） | 拒绝条件 |
| --- | --- |
| `inventory-link-url` | URL解析失败、非法端口格式或fragment |
| `inventory-link-origin` | 协议、主机、端口或用户信息不符 |
| `inventory-link-path` | 路径不符 |
| `inventory-link-query` | 查询语法、键集合或固定参数值不符 |
| `inventory-link-page` | 页号格式、数量或next连续性不符 |
| `inventory-link-syntax` | Link头或参数语法不符 |
| `inventory-link-relation` | rel缺失、多值、未知或重复 |
| `inventory-link-navigation` | 同一响应的next/last关系不一致 |
| `inventory-link-last` | 跨页last改变、提前终止或next越界 |

在获取具体拒绝原因前不凭猜测扩大接受集合。下方记录后续只读调查证据和
据此实施的兼容性修复；未执行初始化、发布或部署，
重跑发布仍须另行授权，不能用重跑试探包状态。

负责人随后批准临时只读Actions诊断：仅在修复分支的受限push事件中运行，
复用现有审计Secret读取身份和包清单，在内存中检查分页响应，
只输出固定分类及结构判断，不输出原始响应或包数据。
该诊断不调用发布入口，不授予包写权限，不合并main；取得结果后移除临时工作流。
本地GitHub CLI凭据的Packages请求返回403，不代表Actions审计Secret无效。
诊断结果如下；临时工作流、诊断脚本及第四工作流合同例外在取证后清理，
不作为永久发布能力保留。

### 实际响应证据

只读诊断运行`43617184884`（提交`844fba2cf01b941e03dcc5c3cfc17711b6f0e930`）
已真实执行：身份校验成功、scope为`read:packages`，首页返回唯一`last`关系，
页号为`0`。路径仍为标准authenticated-user包清单路径，
协议、主机及`package_type=container`、`per_page=100`均匹配，
没有额外查询键或未知关系；因此本次拒绝明确为`inventory-link-page`，
不是路径规范化、Secret缺失或权限不足。

该观测复现了相同校验层的失败，但原运行未保留Link，不能声称恢复了原始响应。
修复后只读诊断运行[`43619979740`](https://github.com/xtianxx/payment-hub/actions/runs/43619979740)
（提交`fe8361f41e9134cfb24cb27ab8c6656334433bf6`）成功，并明确记录
`inventory_empty=true`、`inventory_pages=1`、`inventory_result=accepted`；
实际响应仍为标准路径、唯一`last=0`。这证实了空清单零页终态的真实边界，
不是Secret不生效，也不是路径或额外查询参数不兼容。

修复仅在实际首页数组为空、唯一链接为通过原有URL与查询校验的`last`、
页号精确为`0`时接受为空清单终态，绝不请求page 0。
不能全局允许零页号或忽略分页头；非空清单、后续页、`next=0`、
重复页号、`00`、额外查询参数及其他畸形格式继续拒绝。
HTTP适配器同时保留全部重复Link字段，按逗号合并后交给完整解析器；
不能因字典覆盖丢掉一个`next`而误认唯一`last=0`。
合并后的头仍受原长度限制，重定向拒绝、请求预算和完整分页校验不变。

空清单只证明审计读取结果；正常发布仍因目标缺失而拒绝，
不自动获得初始化许可。当时保留的显式bootstrap创建路径现已按上方当前策略暂停；
owner、包可见性、仓库关联、镜像内容和推送前后验证门禁保持；
包可见性目标以本文件顶部当前策略为准。
只读成功不等于GHCR上传、
首次创建、仓库关联或按digest拉回已通过。

清理后本地验证：`python3 -B -m unittest discover -s scripts -p 'test_*.py'`
通过1085项测试；`scripts/check-ci-contract.py --go <固定Go1.27.4绝对路径>`、
`go test -mod=readonly -run '^TestReleaseGates$' -count=1 -p=1 -timeout=90s ./tests`
及`git diff --check origin/main`均退出0。
两个CI合同文件与main字节一致，最终只保留发布脚本、对应测试与本文档的变化。
这些本地验证不替代新提交的PR/main远端检查。

## GHCR首次初始化修复（2026-11-14）

本节保留`fix/ghcr-bootstrap`修复的背景，并按当前normal-only策略更新操作约束。
首次创建能力曾进入工作流，但已因上方真实发布证据暂停。
后文历史状态保留原始证据，不替代当前规则。

真实运行`43544040888`（源码`e12ebbd8a30fb3a764402c9db52d3bcf7207be90`）的prepare及交接已通过，upload在`Publish paired images`报`package/app-not-found`，未进入login/push。旧流程在首次push前强制要求两个包已存在，且用`/users/xtianxx/packages/container/<name>`查询私有包；该接口文档限定public包，404既不能证明私有包不存在，也不能授予创建资格。

### 正常更新与兼容输入

- **正常发布**：选择`publish`，勾选`packages_verified`并输入`PUBLISH`。两个目标包必须已存在、为public、属于`xtianxx`并关联`xtianxx/payment-hub`。任一缺失、不可见、元数据不符均失败。
- **兼容参数**：publish脚本仍接受`--bootstrap-packages`，但它不允许创建，也不替代两个CLI确认参数。工作流不再传递它。
- 不为private、internal、未关联仓库、owner或名称不符的现存包提供豁免。
- **包与源码仓库的可见性独立**：当前要求GHCR包为public；源码仓库的`repository.private`不决定包自身是否公开，仓库身份仍必须精确匹配。
- prepare不读取包审计Secret、不查询包，也不接收初始化CLI参数。PR和定时检查不获得包凭据或写权限。
- main/SHA/clean checkout、同提交CI成功、原产物交接、load/tag身份、registry manifest、按digest拉回及重新验收保持原有严格要求，不重构建或改用另一对镜像绕过。

### 调用者限制：仅账号所有者

正常更新（含设置兼容开关的调用）只允许`xtianxx`发起，并且重跑发起者也必须是`xtianxx`。`github.actor`与`github.triggering_actor`缺失或任一不符时拒绝，不回退为“原调用者曾获批准即可重跑”。此限制不恢复已暂停的初始化创建能力。

手动操作入口在首步校验操作者、前提复选框和与操作匹配的确认词；upload在job条件中复核两项身份，非owner不启动带`packages: write`的作业。脚本在受信上下文检查中再次验证；正常发布不能创建缺失包。

这是owner单人操作授权，不是多人独立审批。本次不配置GitHub Environment或改变仓库权限；受信工作流内的allowlist不能替代平台级Secret隔离，也不授权通过修改其他分支的工作流绕过策略。以后增加发布者或独立审批须另行确认。

### 凭据准备：只读审计与上传分离

正常更新需要独立的仓库Secret `GHCR_PACKAGE_AUDIT_TOKEN`，不使用仓库级`GITHUB_TOKEN`推断owner命名空间是否缺少某个包。兼容开关不改变凭据分工。

由账号所有者在GitHub网页完成以下操作，**不要把token发到对话、命令参数、Git或日志中**：

1. 在账号设置的Developer settings → Personal access tokens → Tokens (classic)创建有到期时间的token；所有者必须是`xtianxx`，仅勾选`read:packages`。
2. 不增加`repo`、`read:user`、`write:packages`、`delete:packages`或管理权限；按实际需要设置较短有效期并定期轮换、及时撤销。
3. 在仓库Settings → Secrets and variables → Actions中新增repository secret，名称精确为`GHCR_PACKAGE_AUDIT_TOKEN`，值在网页中填写。
4. Secret只注入upload job的`Publish paired images`步骤；不要把它设置成公开变量或注入prepare、PR、全局/job环境。

审计凭据虽只在代码中用于元数据GET，但`read:packages`权限本身允许读取、下载owner的其他私有包，并非只读两个包的元数据。代码限定请求端点、移除子进程继承并避免归档凭据；凭据实际授权范围仍由创建者负责。scope响应头仅作额外检查，缺失不证明只读。

写入和registry验证继续使用工作流`GITHUB_TOKEN`，仅upload job给`packages: write`。该权限包含read，不应重复增加同名`packages: read`配置。审计token绝不用于Docker login/push或registry认证。

### 已存在包判断与每次push后的验证

1. 用审计token查询`GET /user`核对owner，再完整分页读取`GET /user/packages?package_type=container&per_page=100`，不按visibility过滤。
2. 对清单内目标，使用authenticated-user接口`GET /user/packages/container/<name>`严格校验public、仓库关联、owner、name、type和package ID。继续使用完整owner清单，不因目标公开而退回public-only接口判断不存在。
3. 两包都必须已有有效元数据和稳定ID，才能继续发布准备；任一缺失即拒绝。403、404、超时、分页不完整、畸形响应及任何未知状态均失败。
4. 每个role push前刷新清单；已存在包消失或ID改变都停止，不自动把更新切换为创建。push后继续核对原ID，不将缺失状态提升为新创建包。
5. 每次push后立即复查public及仓库关联等元数据；失败即停止，不继续第二个包。最终再次核对两包，并保留原digest/拉回/验收链路，全部通过才记为发布成功。

分页结束遵循GitHub的`Link`契约：无后续链接可表示终页，恰好100条的终页也合法，不能仅凭返回数量认定缺页。已声明未来last却缺少续链、畸形链接或跨页last不一致会失败；这不保证检测到服务端违反契约或所有并发位移。

### 必须接受的剩余风险与失败处置

历史初始化曾在负责人接受非原子风险后实现，但现已暂停，不能继续据此批准创建。
完整分页、push前重查和push后验证都不是原子快照；外部写者仍可能在检查与push之间删除、重建包或改变visibility。正常更新也需受控窗口，不承诺原子安全。

push后检查只能检测问题，不能保证阻止首次暴露；包一旦public不能直接改回private。排他操作窗口仅降低风险，不提供零风险保证。

摘要区分“已尝试push”“远端效果未知”和“验证成功”，不以`pushed_roles`为空宣称没有副作用。即使失败、取消或摘要未能生成，也可能已创建包或上传部分内容。出现部分成功或未知状态时停止并人工核对，不自动重试、删除、改public、恢复包或回滚。不得声称原子创建成功或自动恢复到发布前状态。

这次源码变化会使旧候选清单失配，后续真实发布必须构建修复后SHA的新候选；不能复用诊断运行的旧bundle。

### 发布摘要v1的兼容规则

`payment-hub.release-summary.v1`的对象字段允许向后兼容地追加；消费者应读取其需要的已知字段，并忽略未知字段，不把旧字段集合当作封闭schema。本次新增审计、初始包状态、push尝试及副作用字段，不删除或改变原有字段的类型与含义；不能只检查旧`pushed_roles`就推断没有远端副作用。

后续若需删除既有字段、改变其类型或含义，应评估并升级schema版本及迁移方式。历史v1摘要不重写；仅新增字段不自动升级为v2。仓内当前消费者按字段读取，未发现release-summary封闭字段集消费者；此检索不保证仓外不存在自行实现的严格消费者。

`limits`是描述当前实现边界的开放风险标识列表，不是封闭枚举、成功状态或授权条件；
标识可随实际策略更新，消费者应保留未知项用于风险审阅，
不能据某项缺失或出现未知项就将失败当作成功。
当前公开包策略用`package-visibility-check-not-atomic`描述可见性检查的非原子性，
并用`pair-publication-not-atomic`明确成对发布不是原子事务，
不再生成已过时的`initial-default-private-contract-not-atomic`。
历史摘要及其风险标识不改写；保持v1不等于保证仓外精确匹配旧列表的实现完全不受影响。
完整发布的成功判断以顶部消费门禁为准，不以`limits`列表推断。

### 官方依据

- [Authenticated-user包清单与包元数据接口](https://docs.github.com/en/rest/packages/packages)
- [GitHub REST分页与Link结束信号](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)
- [认证用户身份](https://docs.github.com/en/rest/users/users#get-the-authenticated-user)
- [GHCR首次private、source label与工作流关联](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [包可见性、仓库访问继承与public不可逆](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

## 发布环境修复（2026-11-13，历史记录）

真实Actions运行`43371460609`的prepare job在`Prepare release handoff`失败，日志为`image/descriptor-missing`，后续上传job跳过。失败发生在构建后的镜像身份检查，不是GHCR认证失败；没有产生成功发布结论。下方2026-11-12记录保留为历史证据，不再代表Actions从未运行。

本次修复为prepare和upload两个job配置相同的独立Docker daemon，固定Engine **29.9.3**并显式启用containerd image store；不依赖runner预装Docker的版本或存储默认值。官方setup action固定commit，显式不传GitHub token；匿名版本元数据请求仍可能受限流影响，失败时停止，不借用发布凭据重试。原有OCI index、归档和镜像身份校验保持不变，不用缺少`Descriptor`时降级为其他摘要来绕过门禁。

环境检查在耗时构建/导入前核对版本、存储类型和daemon身份，并验证临时空`DOCKER_CONFIG`仍连接同一daemon。必须保留`set-host: true`导出的`DOCKER_HOST`：publish会切换到隔离认证目录，仅设置Docker context不足以维持连接。

29.9.3是本次固定的安全补丁版本；历史本地save/load证据来自29.6.1，不能冒称29.9.3已经通过真实往返验证。本次本地合同/回归检查不等于修复已在GitHub runner生效，也不证明GHCR上传与按digest拉回成功；真实工作流复验仍待修复提交后另行授权。本次不自动提交、推送、重跑工作流或发布。

### 合并前预检与正式发布的分工

`ci.yml`在目标为main的PR及push main时运行配对镜像作业：先构建并验收原始配对镜像，
main在镜像标签仍存在时从同一对镜像导出四文件bundle，
再调用`check-image-handoff.py`验证save/load内容身份；
预检成功后才上传bundle及pointer，供完整CI成功后选取。PR只上传安全摘要。
它不登录GHCR、不申请包写权限，也不将PR产物当作发布候选。
镜像移除仅限确认归属的隔离临时daemon，禁止影响普通本地daemon及无关资源。

`release.yml`仍为main人工操作，不执行镜像builder。
默认选择`publish`；两种操作都要确认各自的前提并输入对应的确认词，
`initialize-migrate`的确认词明确提示首次可能公开。
两种写入job互斥。初始化只写迁移包，不能作为完整配对发布成功。
候选解析和真正发布入口前均由`check-main-ci.py`核验同SHA的`ci.yml`最新push/main成功运行，
并锁定run ID和attempt。精确选取并校验该运行的pointer及bundle artifact ID/digest，
再用受信descriptor SHA执行`verify-handoff`，不取全仓最新同名产物，
不从PR、nightly、旧成功或旧attempt回退。产物缺失、过期或来源不符直接停止。
查询和下载只读使用`GITHUB_TOKEN`及`actions: read`；
包写权限只属于正常upload及独立初始化job；
audit Secret仅在对应publish或初始化执行步骤显式注入，不写摘要。
权限本身是job级，不能把显式env注入限制表述为step级包写权限隔离。

建议顺序：修复分支开PR → `ci.yml`及其全部必需检查通过 → 合并main →
合并后SHA的push/main `ci.yml`及其全部必需检查通过 → 核实包权限并在另行授权后人工发布。
两个精确必需检查名称仍为`CI gate`与`Paired image acceptance (linux/amd64)`，
不是要求两个自动工作流；nightly定时运行，release人工触发。
PR绿灯不能代替main合并提交绿灯；发布门禁不会自动等待、重跑CI或改变远端规则。

每日漏洞扫描通过`nightly.yml`的schedule运行，不触发发布、不生成候选，
也不替代上述同SHA push/main检查。CI原有漏洞门禁继续保留。
定时扫描成功不是发布绿灯；GitHub调度可能延迟，不保证准点。

历史环境修复记录当时发现main缺少必需状态检查，不代表当前服务端状态。
2026-11-16合并前已只读核对active ruleset：
`CI gate`与`Paired image acceptance (linux/amd64)`均为必需检查，
严格最新main基线要求已启用；本次没有修改远端规则。
合并时仍须重新核验这些保护及精确head的检查结果。
真实归档往返、跨run交接与GHCR全链分别保留证据，不能用本地fake相互替代。

### 交接预检的取消边界

交接预检收到SIGTERM/SIGINT后，停止后续操作并通知当前自有客户端进程组；宽限期后仍存活则强杀、有限等待回收，摘要记录固定的`interrupted/sigterm`或`interrupted/sigint`失败及回收状态。重复取消信号不会中断该清理。只有确认客户端已回收后，才删除本轮独占临时目录内的`pair.tar`；不删除builder已有报告或无关文件。

这只保证客户端进程组的有界监督，不声称撤销Docker daemon已经接收的save/load等操作，也不自动回滚镜像状态。无法确认回收时保留归档并明确记录失败。真实子进程测试用Python替身验证忽略信号的子/孙进程及重复取消，不等于已在真实Docker中验证取消或回滚。

## 历史本地状态（2026-11-12）

交付审查后，发布委派清理与工具timeout分类另行修复；其最新范围接受以`artifacts/phase6/delivery-review-fixes/acceptance.json`为准，不由下方历史正常路径绿灯替代。此次脚本变更会使旧全仓库镜像清单失配，未自动重构建或重贴旧镜像。受控进程测试与真实GHCR发布仍是不同证据，外部操作继续未授权/未验证。

用户已确认按首版收口本地范围：没有早期已发布应用/schema基线，旧版本升级兼容检查本次不适用，不记为测试通过；未来升级必须另验。本决定不解除真实发布、包权限、Actions或部署的授权与验证门禁。

最新本地门禁记录：`artifacts/phase6/final-validation/run-20261112T033948Z-67d8d604/acceptance.json`。源码级builder已实际执行，新候选应用/迁移各构建一次并通过原产物验收，两个成功摘要已产生；修改工作树明确记录`source_dirty=true`、`source_sha=null`，不冒充main上的干净提交。受控发布回归与三工作流静态合同通过，但真实prepare/publish全链、Actions、GHCR及部署仍未执行。首次漏洞工具失败保留，另批一次诊断扫描有效通过，首次根因未知。以下历史镜像ID保留为历史pair，不用它替代最新候选；最新ID以该记录引用的`candidate/build-record.json`为准。

本文件区分本地验收、工作流配置、真实上传、按摘要拉回和部署。任一层通过都不自动证明后一层通过。

- 仓库：`xtianxx/payment-hub`，地址`https://github.com/xtianxx/payment-hub`；私有，默认分支`main`，owner类型为User。本次现有账号只读查询返回ADMIN，但不把仓库管理权限当作GHCR包权限。
- T070安全回归和T071–T075配对镜像已本机接受。权威记录：`artifacts/phase6/local-image-acceptance.json`及其引用的独立验证报告。
- 镜像验证源码清单的完整摘要见`artifacts/phase6/image-validation/run-20261112T013518Z-d2f9a4b4/build-record.json`，不能凭下文短标签反推完整摘要。
- 本地应用标签：`payment-hub-app:phase6-fbe6f7574120383fa6a6f751`；本地迁移标签：`payment-hub-migrate:phase6-fbe6f7574120383fa6a6f751`。两者各构建一次，测试使用原产物，无上传。
- 本地应用ID：`sha256:ede3973ea6bbacb470531a12a755caf530fec93e4b709636ffff1e580974f123`；迁移ID：`sha256:e37f8d2fc1c93c6b3fa7010ea2a4723b1cab4e9f880ca5661eeffa1b770ebd1c`。它们不是registry manifest digest。
- 真实GHCR上传、包关联、可见性、按digest拉回和部署均未验证。当前没有远端配对摘要，不能把本地ID填入远端摘要字段。

镜像验收后的指南和任务文字已有更新；全仓库源码清单因此不再等于当时构建清单。历史原产物及证据保留，不重贴标签、不自动重构建，不把历史验收当作当前源码候选通过。未来固定源码或产物发生改变，必须拒绝沿用旧候选的验收。

## 固定输入与本地运行边界

沿用Go1.27.4、原`go.mod`/`go.sum`、PG16.15、Goose v3.29.0及现有精确基础镜像摘要，不使用`latest`或自动升级。`deploy/Dockerfile.app`构建CGO0静态应用，宿主race检查仍使用CGO1；同一应用镜像同时用于normal与test模式，不为模式切换重构建。

PG-only基础文件为`deploy/compose.yaml`与`deploy/compose.test.yaml`。应用及一次性迁移使用可选`deploy/compose.app.yaml`叠加文件和固定profile `app`、`app-test`、`migrate`；支持的操作明确选择服务，不用裸up自动带起应用或迁移。正常应用不挂测试socket；测试应用仅挂本次私有目录，不挂Docker socket，不放宽为root或0777。

迁移必须显式执行，应用启动不自动迁移；迁移失败或应用启动失败应停止并读取固定分类诊断，不自动清库、删演示卷或重付。正常停止/重启与显式清理测试资源分开。不同源码版本与旧镜像的兼容性必须单独检查，不能以本次镜像可启动推断旧版本兼容。

预算：原120秒integration包、570秒Go批次及业务期限不变。独立镜像Go阶段180秒，到期即失败；另批最多15秒仅收割测试进程，不继续业务或重试。PG准备、迁移及Docker归属清理使用各自既有有界预算。

## 历史包关联、可见性与权限记录（2026-11-12）

现有gh凭据的scope不含`read:packages`。只读查询`user/packages?package_type=container`返回403，因此不能确认包是否存在、是否关联仓库、可见性或读写授权。猜测包名返回404也不能证明私有包不存在；REST没有仓库级package列表端点，不使用错误路由的404作不存在证据。

未创建包、未执行login/PAT认证、未变更visibility或仓库/包权限。首次GHCR发布默认private，但现有包的实际visibility不能从仓库private推断。通过CLI推送同名标签也不等于包已关联仓库；需要核对包设置或正确的来源关联。已有但未关联仓库的同名包可能拒绝`GITHUB_TOKEN`上传。

后续若用户决定真实发布，先由仓库负责人通过GitHub包设置核对：

1. 目标namespace、应用和迁移两个包的准确名称及是否已存在。
2. 两包与`xtianxx/payment-hub`的关联、实际visibility、访问继承及Actions仓库读写授权。
3. 上传与按摘要拉回所用账号/token的对应权限；不为绕过限制借用PAT或公开镜像。
4. 在批准具体外部动作前保持“未核实”，不能先推送试探权限。

如需CLI读取包元数据，`read:packages`是对应权限前提；本文件不授权扩充scope或新建token。认证值仅经受控环境或password-stdin传递，不写命令参数、报告、日志、请求归档或Git。

## 受控发布必须具备的门禁

以下为受控发布必须保留的约束；实现入口见下文，是否完成真实发布须另有执行证据：

- 只允许默认分支受信ref的人工触发，固定并复核源码SHA；`workflow_dispatch`本身不保证main-only。
- 在job内拒绝非`refs/heads/main`，不执行不可信PR代码的`pull_request_target`，不接受任意ref替换固定SHA。
- 检查、构建一次成对镜像、验收原产物，再交接上传；任一检查失败即候选不可用，不能上传前重构建另一对后冒用旧验收。
- 普通检查与构建只用`contents: read`。只有实际上传job给`packages: write`，基础上传不额外申请OIDC或证明签发权限。
- 上传完成后分别记录两个最终registry摘要，按摘要拉回核对并运行；失败不能标记“配对发布成功”。真实上传与digest拉回都需另行授权，不能由本地脚本测试代替。
- tag可移动，仅用于查找；源码SHA、本地image ID、registry manifest digest分别记录。缺任何必要映射或产物改变，拒绝旧候选交接。
- 不自动部署；发布成功也不构成部署授权。

## 产物保留

只读API`repos/xtianxx/payment-hub/actions/permissions/artifact-and-log-retention`返回`days=90`、`maximum_allowed_days=400`。这是当前仓库有效配置/上限，未独立核实其他企业级限制。30天目标在已知上限内。

已固定的upload-artifact版本还声明1–90天的action参数范围；
仓库上限与action参数上限是不同限制，30天同时满足两者。
下载使用已固定的`actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333`
（v8.0.2），与upload v7兼容。
当前release按选定main CI的run ID和精确artifact ID跨run下载，使用`actions: read`
及`GITHUB_TOKEN`，不使用PAT或跨仓库来源。
下载完整性与可信pointer给出的descriptor SHA、归档和镜像身份校验均须通过，
不能仅相信下载bundle内自报的expected值。旧prepare到upload的同run交接属于历史接口，
不代表当前候选流程的权限、来源或哈希取得方式。

历史T077记录当时只核对了远端`setup-check.yml`的14天配置，
其本地30天改动尚未推送/运行；这是当时的验证边界，不代表当前工作流仍只有旧文件。
新工作流请求保留30天，实际Actions运行后仍须用artifact的`expires_at`复核；
修改YAML不改变已有对象的到期日。构建/发布产物及诊断都不是永久存储。

普通检查job只上传白名单重建的报告。main CI还生成固定原镜像交接bundle
（两镜像单一归档、descriptor、原构建与验收记录）及小型pointer；
release只下载这些已验收产物，不重新构建。它们会占用artifact存储，按30天请求保留，
真实有效期仍须核实API的`expires_at`。来源限定精确run/attempt/artifact ID，
两类产物都排除凭据、原始stdout/stderr、全量环境、DSN、Authorization和数据库导出；
采集或核验失败即停止，不扩大归档范围，也不在release重建补救。

## 官方依据与停止条件

- [GHCR使用与默认可见性](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [Actions发布/安装包及仓库关联](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions)
- [包访问与visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)
- [workflow_dispatch边界](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
- [产物保留配置](https://docs.github.com/en/actions/tutorials/store-and-share-data#configuring-a-custom-artifact-retention-period)

仓库身份或默认分支变化、包权限未知、source/产物不匹配、缺必要摘要、任一关卡失败、保留目标超过实际限制或缺外部动作授权，均应停止对应操作并报告，不能以临时放宽权限、改公开或跳过门禁绕过。

### 原产物交接的本地实验证据

`artifacts/phase6/handoff-probe/verification.json`记录一次save和一次load，未重构建、未触网。首次以整个Docker Descriptor字典比较而失败，诊断确认load增加了本地引用annotations；原ID、根mediaType/digest/size、归档中原始根manifest字节hash、linux/amd64及源码label保持一致。首次失败记录保留，没有重跑来覆盖它。发布门禁应比较这些内容身份字段，不能把可变引用annotations当作内容变化，也不能假定本地ID必然等于config digest：本环境的ID对应OCI index。

未来远端摘要必须来自registry实际GET的原始manifest字节及返回digest核对，再按摘要拉回、核对原产物并运行。现在的本地descriptor或RepoDigests都不是GHCR访问/存在证据；缓存命中的pull也不能证明一个全新客户端可完整下载全部远端blob。

### 实现入口与尚未执行项

已实现`release.sh --stage prepare|verify-handoff|publish --source-sha <40hex>`；它不是本地任意工作树的一键发布。prepare要求受信workflow_dispatch、main、固定SHA与clean checkout一致，并串行运行必要检查后调用共同builder。当前未提交工作树不能通过该受信门禁，不得提交代码或伪造CI环境来绕过它。

该prepare入口保留为历史受控接口，不由新的`release.yml`调用。
当前CI通过`ci-candidate.py`从本轮builder原产物导出bundle，
CD通过`select-release-candidate.py`绑定可信来源后调用verify-handoff及publish。

verify-handoff只验证固定四文件bundle，必须另传受信来源的
`--expected-descriptor-sha256`。当前`release.yml`从选定main CI运行的可信pointer取得该值，
经selector核对run/attempt、源码与bundle artifact身份后再使用；
不能取下载bundle自身提供的expected值，也不应寻找当前流程未生成的prepare输出。
仅在使用保留的历史prepare接口时，expected值才来自该次可信prepare输出。
publish仍要求显式`--approve-external --package-preconditions-verified`、
真实包前提检查及受信上下文；程序门禁不替代用户对外部动作的授权。
历史prepare结果或本地受控回归均不能替代当前跨run候选交接、
GHCR认证/上传及按digest拉回的实际证据。

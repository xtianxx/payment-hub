# CI、Nightly与受控发布

本分支将工作流职责调整为三个文件。该调整不授权GHCR上传、包创建、部署、
远端保护变更或历史任务重跑；真实跨运行镜像交接仍需新提交的Actions证据。

## 职责

| 工作流 | 触发 | 本轮职责 |
| --- | --- | --- |
| `ci.yml` | 目标main的PR、push main | Format、Vet、显式Go Build、Unit Tests、Integration Tests、配对镜像验收、漏洞扫描、CI Gate |
| `nightly.yml` | 每日定时 | 漏洞扫描；不生成发布候选、不发布 |
| `release.yml` | main人工触发 | 权限与同SHA main CI核验、获取原CI候选、交接与身份校验、受控GHCR发布、结果摘要 |

`release.yml`属于CD的发布阶段，不是纯CI，也不自动部署。
CI与nightly都保留漏洞扫描：定时成功不能替代当前源码的CI成功，
CI漏洞作业失败仍阻断CI Gate。

本轮不实现较长并发测试、数据库升级兼容性测试或故障恢复实验，
也不注册空任务、占位命令或用skipped表示它们已通过。
后续需分别确认并发规模、预算、旧schema/旧镜像基线、故障模型与恢复成功条件；
当前短并发回归、fresh数据库迁移及受控取消检查不能冒充这些实验。

## CI检查与产物

Format、Vet、Build是独立的真实检查。Go Build不替代测试，也不重复构建发布镜像。
保留原有单元race、数据库集成、真实fixture清理及镜像内容验收要求。

以下服务端必需检查名称保持不变：

- `CI gate`
- `Paired image acceptance (linux/amd64)`

配对镜像作业移入`ci.yml`，但不改其检查名称。CI Gate同时依赖镜像验收和其他
必需CI作业；失败、取消、未执行或未知状态不当作成功。
工作流路径改变不等于服务端保护已验证，实际合并前仍按Git规范核对。

镜像作业仅调用一次配对builder并验收原镜像；main先从这同一对镜像导出
四文件bundle，再执行save/load交接预检，预检成功后才上传候选：

```text
pair.tar
descriptor.json
build-record.json
accepted-images.json
```

导出先于预检，是因为预检会受控移除并按原ID加载镜像，只保证内容ID恢复，
不保证原builder标签恢复。候选上传必须晚于预检成功，
不能因bundle已在本地生成就绕过预检或宣布可发布。

只有本仓库的push/main作业可以导出并上传发布候选；PR、fork PR和nightly
不能提供release可选的正式候选。其他CI作业失败时，即使bundle已经上传，
该次运行也不具备发布资格。
导出及pointer入口还须核对GitHub自动设置的`GITHUB_WORKFLOW_REF`，
精确为`xtianxx/payment-hub/.github/workflows/ci.yml@refs/heads/main`；
不能仅根据push/main事件，就把其他工作流自报为`ci.yml`来源。

bundle和pointer的名字绑定CI run ID与attempt，不使用浮动的“最新同名产物”：

```text
ci-release-bundle-<run_id>-<run_attempt>
ci-release-pointer-<run_id>-<run_attempt>
```

pointer只包含固定来源标识、源码SHA、平台、bundle artifact ID及digest、
descriptor SHA。它必须来自选定的可信CI运行，不能把任意本地pointer或bundle内
自报的descriptor哈希当作来源证明。归档只包含交接白名单内容，
不包含运行时凭据、全量环境或敏感原始日志。

## release只提升已验收产物

1. 仅可信仓库的main人工触发；原始及重跑actor均须为`xtianxx`，
   原有两项发布确认仍必须满足。
2. 查询当前源码SHA对应`ci.yml`最新push/main运行，要求已经完成且成功。
   PR、nightly、其他SHA、旧成功以及最新失败或进行中的运行不能替代。
3. 锁定CI run ID和attempt，按精确artifact ID下载对应pointer；
   对pointer与bundle元数据检查唯一性、未过期、名称、来源及digest。
   完成artifact元数据选择后，再以同一run/attempt锁复核最新成功CI，
   不把读取运行列表时的旧快照默认为仍然最新。
4. pointer字段与选定运行、源码、bundle artifact ID及API digest逐项匹配后，
   才按精确ID下载原bundle。产物下载和解析位于独占临时目录，不执行产物内代码。
5. 使用受信pointer中的descriptor SHA执行原有`verify-handoff`，
   验证源码、四文件、归档和两镜像身份；在真正publish前重新核验同一CI run/attempt。
   出现新运行或attempt时停止，不静默换包继续。
6. 继续原有load后身份核验、包元数据前后复查、registry digest、按digest拉回及再验收。
   最后记录同一运行的发布摘要；发布成功仍须满足完整镜像对消费门禁。

release不执行builder，也不重新构建镜像来替代丢失、过期或失败的候选。
需要新候选时，先在另行授权下运行可信CI，重新等待该源码的完整CI成功。
下载的bundle摘要、源码SHA或运行来源不符时直接拒绝。

解析与下载阶段仅使用仓库只读内容和Actions读取权限；
包写权限只在上传job，审计Secret只注入发布步骤。
审计token不用于Docker或registry认证，发布凭据不传给构建及其他子进程。

## 未改变的发布边界

- 两个固定目标必须已经存在、为public、owner和仓库关联正确，原包ID稳定。
- 缺失包自动创建继续暂停；`bootstrap_packages`只作兼容输入。
- 不修改包设置、不初始化迁移包、不用占位镜像创建包。
- 不把匿名可下载、单个标签存在或半次push成功当作完整发布成功。
- 取消、失败或事后校验失败不撤销已产生的远端副作用，不自动重试或回滚。
- 最后检查与外部写者操作、CI运行更新之间仍非原子；不声称本次拆分消除了竞态。

历史发布run仍使用其原始代码和交接方式。禁止用旧run重跑来“应用”本次重构；
上线前需检查旧发布运行已结束，并使用新main及其新CI候选。

## 首次本地验证（2026-11-16，审查修复前）

以下均在`refactor/ci-nightly-release`实际执行，退出码均为0：

| 命令 | 结果 |
| --- | --- |
| `python3 -B -m unittest discover -s scripts -p 'test_*.py'` | 1085项通过；包括来源错配、跨模块pointer交接、跳过/吞错/Secret及顺序mutation回归 |
| `python3 -B scripts/check-ci-contract.py --go <固定Go1.27.4绝对路径>` | `ci-contract: pass` |
| `go vet -mod=readonly -p=1 ./...` | 通过 |
| `go vet -mod=readonly -p=1 -tags=integration ./...` | 通过 |
| `go build -mod=readonly -p=1 ./...` | 通过 |
| `bash scripts/release.sh --help` | 发布入口参数解析通过，未执行发布stage |
| `git diff --check` | 通过 |

Go命令使用本机固定Go1.27.4、隔离配置及独占构建缓存，关闭模块网络访问；
工具链路径为`/home/dream/go/pkg/mod/golang.org/toolchain@v0.0.1-go1.27.4.linux-amd64/bin/go`。
本机日志索引分别为`/tmp/opencode/ci-final-validation-65hm0cgy/results.json`
和`/tmp/opencode/ci-go-split-validation-sjufwq5e/results.json`，不作为仓库交付产物。

以上不证明真实Actions表达式执行、跨run下载、main候选产出或GHCR发布成功。
本轮未重跑真实数据库集成、Docker配对构建/验收及在线漏洞扫描；
Python回归中的fake拒绝场景也不等于这些真实外部链路通过。
未提交、推送、创建PR、修改远端保护、初始化包或触发正式发布。

## 审查意见的适用边界

拆分链为#18候选导出、#19可信候选选择、#20工作流切换。
#19中`ci.yml`尚不存在是已定义的接线阶段：旧工作流不调用新选择器，
第三步才引入`ci.yml`并接线。不能为消除此阶段的404而允许旧工作流
或旧成功记录成为候选来源。

run/attempt锁只适用于单一`ci.yml`候选源；过渡阶段旧双工作流的
无锁默认门禁仍须同时检查两项，不能给两个独立run套同一个锁。
测试必须分别证明真实生产默认值与显式候选源，而非覆盖常量后自证。

摘要持久化失败时不得发布成功的`GITHUB_OUTPUT`值。
摘要与输出不是跨文件原子事务；输出写入失败也必须让步骤失败，
正常消费者仍须依赖前置步骤成功，不得用`always()`绕过候选失败。
元数据后的复核缩小新run/attempt出现的窗口，不声称消除了外部状态竞态；
真正publish前的同run/attempt复核继续保留。

`docs/git-workflow.md`恢复本次工作流重构前的原文；
技术职责与交接变化只在本说明、开发及发布文档记录，不虚构对协作规范的额外授权。
首次1085项测试记录不作为审查修复后的通过证据。

### 审查修复后的本地验证（2026-11-16）

- `python3 -B -m unittest discover -s scripts -p 'test_*.py'`：
  1112项通过，退出码0。新增反例覆盖输出换行注入与路径重叠、真实工作流来源、
  record/accepted验证后突变、精确inspect调用、artifact读取期间的新run/attempt、
  摘要及输出写入失败、独立生产默认值和明确的事件拒绝原因。
- `python3 -B scripts/check-ci-contract.py --go <上述固定Go1.27.4绝对路径>`：
  `ci-contract: pass`，退出码0。
- `bash scripts/release.sh --help`、`git diff --check`均退出码0。
- `git diff --exit-code 43cda8f6717b9a795b3bc99002c5c770b7b2c9a2 -- docs/git-workflow.md`
  退出码0，确认协作规范恢复原文。
- 本机证据：`/tmp/opencode/pr-review-fix-final-8a3eici1/results.json`。
  验证期间逐文件哈希保持不变；本节验证记录随后补入。

上述1112项记录是首次审查修复的本地合并态验收快照，当时尚未提交或更新PR。
随后按#18→#19→#20追加提交同步修复，不改写已推送历史。
#19保留旧双工作流默认门禁，明确选择单一候选源，
并以独立生产模块断言代替覆盖常量后的自证；
#20才切换最终默认值，不能把最终默认值提前套用到#19。
追加输出还覆盖已有文件缺少末尾LF的情形：只在必要时补一个分隔换行，
保留旧值及独立输出键；选择器失败回退也移除本次补入的换行。
本地验收与远端审查/CI状态不同，当前head及更新后的实测结果见各PR说明，
不能沿用旧head的检查或批量标记审查已解决。
真实Actions、跨run下载、Docker配对验收、在线扫描和GHCR发布仍未因此获得通过证据。

### 有序PR追加修复验证（2026-11-16）

包含新增的“已有输出缺少末尾LF”反例后，三阶段分别执行
`python3 -B -m unittest discover -s scripts -p 'test_*.py'`：

| 阶段 | 通过项数 | 本机证据索引 |
| --- | --- | --- |
| #18候选导出 | 1111 | `/tmp/opencode/pr-update-18-1ckil1yf/results.json` |
| #19旧默认兼容及候选选择 | 1219 | `/tmp/opencode/pr-update-19-0nauc6uo/results.json` |
| #20最终工作流接线 | 1118 | `/tmp/opencode/pr-update-20-uqa247bk/results.json` |

三阶段固定Go1.27.4工作流合同及diff检查均退出码0；
最终阶段还验证发布入口帮助和协作规范原文恢复，退出码均为0。
项数差异包含旧工作流合同与过渡专属测试，不代表跳过安全检查。
各阶段验证期间脚本文件哈希不变，本节随后补入；远端CI与审查结果仍须绑定最新PR head另行核对。

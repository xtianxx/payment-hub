# 本地镜像与受控发布

## 当前状态（2026-11-12）

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

## 包关联、可见性与权限：尚未核实

现有gh凭据的scope不含`read:packages`。只读查询`user/packages?package_type=container`返回403，因此不能确认包是否存在、是否关联仓库、可见性或读写授权。猜测包名返回404也不能证明私有包不存在；REST没有仓库级package列表端点，不使用错误路由的404作不存在证据。

未创建包、未执行login/PAT认证、未变更visibility或仓库/包权限。首次GHCR发布默认private，但现有包的实际visibility不能从仓库private推断。通过CLI推送同名标签也不等于包已关联仓库；需要核对包设置或正确的来源关联。已有但未关联仓库的同名包可能拒绝`GITHUB_TOKEN`上传。

后续若用户决定真实发布，先由仓库负责人通过GitHub包设置核对：

1. 目标namespace、应用和迁移两个包的准确名称及是否已存在。
2. 两包与`xtianxx/payment-hub`的关联、实际visibility、访问继承及Actions仓库读写授权。
3. 上传与按摘要拉回所用账号/token的对应权限；不为绕过限制借用PAT或公开镜像。
4. 在批准具体外部动作前保持“未核实”，不能先推送试探权限。

如需CLI读取包元数据，`read:packages`是对应权限前提；本文件不授权扩充scope或新建token。认证值仅经受控环境或password-stdin传递，不写命令参数、报告、日志、请求归档或Git。

## 受控发布必须具备的门禁

以下为待实施/验证的发布约束，不表示当前已有可执行发布命令：

- 只允许默认分支受信ref的人工触发，固定并复核源码SHA；`workflow_dispatch`本身不保证main-only。
- 在job内拒绝非`refs/heads/main`，不执行不可信PR代码的`pull_request_target`，不接受任意ref替换固定SHA。
- 检查、构建一次成对镜像、验收原产物，再交接上传；任一检查失败即候选不可用，不能上传前重构建另一对后冒用旧验收。
- 普通检查与构建只用`contents: read`。只有实际上传job给`packages: write`，基础上传不额外申请OIDC或证明签发权限。
- 上传完成后分别记录两个最终registry摘要，按摘要拉回核对并运行；失败不能标记“配对发布成功”。真实上传与digest拉回都需另行授权，不能由本地脚本测试代替。
- tag可移动，仅用于查找；源码SHA、本地image ID、registry manifest digest分别记录。缺任何必要映射或产物改变，拒绝旧候选交接。
- 不自动部署；发布成功也不构成部署授权。

## 产物保留

只读API`repos/xtianxx/payment-hub/actions/permissions/artifact-and-log-retention`返回`days=90`、`maximum_allowed_days=400`。这是当前仓库有效配置/上限，未独立核实其他企业级限制。30天目标在已知上限内。

已固定的upload-artifact版本还声明1–90天的action参数范围；仓库上限与action参数上限是不同限制，30天同时满足两者。后续同轮交接使用已核验的`actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333`（v8.0.2），与现upload v7兼容。按本轮精确artifact ID下载，不指定跨run/repo或PAT；同run使用平台runtime token，不增加`actions: read`。下载校验与prepare可信输出的descriptor/归档哈希校验都必须通过，不能仅相信下载包内自报摘要。

当前已核的远端只有`setup-check.yml`，其三处上传配置仍为14天。本地T077配置已把这三处及新增`check.yml`的安全摘要设为30天，但未推送/运行，不能宣称远端已生效或既有产物保存30天；实际Actions运行后仍须用artifact的`expires_at`复核。仅修改本地YAML不改变已有对象的到期日。构建/发布产物及诊断都不是永久存储。

普通检查job只上传白名单重建的报告。手动发布草案的只读prepare job另有固定的原镜像交接bundle（两镜像的单一归档、descriptor、原构建与验收记录），这是跨job移交原产物，不是放宽诊断日志归档；必须排除运行时凭据并限制为同轮精确artifact ID。它会占用额外artifact存储，只有后续获授权的人工工作流实际运行才产生该占用。两类产物都不包含原始stdout/stderr、全量配置/环境、DSN、Authorization、完整报文或数据库导出；采集失败应导致门禁失败，而不是扩展敏感归档范围。

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

verify-handoff只验证固定四文件bundle，必须另传来自可信prepare输出的`--expected-descriptor-sha256`，不能使用下载包自身提供的expected值。publish还要求显式`--approve-external --package-preconditions-verified`、真实包前提检查及受信上下文；这些参数是程序门禁，不替代用户对外部动作的授权。当前真实prepare全链、GHCR认证/上传/按digest拉回均未执行，受控回归不能解除上述未验证状态。

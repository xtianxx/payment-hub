# 迁移包一次性初始化

本入口只解决`ghcr.io/xtianxx/payment-hub-migrate`尚不存在的问题，
不替代完整镜像对的正常发布，不部署应用，也不执行真实业务数据库升级。
仍使用`release.yml`，不增加第四个工作流。

## 首次可见性与授权

**不能保证首次推送先保持private。** 使用工作流`GITHUB_TOKEN`创建包时，
GitHub的Actions规则可能使包继承源码仓库的可见性和权限模型。
本项目历史app首次推送后已出现公开可读的结果，不能仅依赖通用的“首次默认private”描述。

负责人已确认按“首次可能直接公开”设计本入口。
每次执行仍须独立确认这一风险，不能用事后检查替代事前授权：
public包不能直接改回private，已经被外部下载的内容无法撤回。
初始化只使用已通过可信main CI验收的真实迁移镜像，不推送空镜像、占位镜像或新构建的替代品。

本轮已授权本地实现、测试及Git提交，不包含Git推送、合并、
首次镜像推送、包创建、可见性修改、正式发布或部署。
下文执行步骤仅供后续另行授权时使用。

## 与正常发布互斥

`operation`默认是`publish`，初始化确认默认全部关闭。
两种操作共用只读候选解析，但只有相应的写入job可以执行。

| 输入 | 正常发布 | 初始化迁移包 |
| --- | --- | --- |
| `operation` | `publish` | `initialize-migrate` |
| `publish_authorized` | `true` | `false` |
| `packages_verified` | `true` | `false` |
| `bootstrap_packages` | 建议`false`；旧兼容值不创建包 | 必须`false` |
| `initialize_authorized` | `false` | `true` |
| `initialization_preconditions_verified` | `false` | `true` |
| `public_creation_acknowledged` | `false` | `true` |

未知操作、缺少任一初始化确认、同时勾选两种模式的授权、
或试图用旧bootstrap开关创建包，均拒绝执行。
初始化与正常发布仍要求本仓库main人工触发，原始actor与重跑actor均为`xtianxx`。

正常发布的两包现存、public、owner、仓库关联及原ID门禁不变；
初始化不会给正常publish增加允许缺包、跳过public或任意角色/目标的选项。

## 初始化前提

执行前由owner确认维护窗口，没有其他工作流、本地客户端或外部自动化写入两个目标包。
工作流concurrency只能约束本工作流，不能锁住所有外部写者。

程序必须再次确认：

- app包已经存在、为public、属于`xtianxx`且正确关联`xtianxx/payment-hub`，并冻结其ID。
- migrate在完整可信清单中确认为缺失；已存在的public、private或异常对象都不是初始化目标。
- 当前SHA最新push/main CI成功，run、attempt、bundle artifact ID及digest与已选候选一致。
- pointer、descriptor、四文件bundle、源码、平台及两镜像内容身份全部匹配。
- 所有输入和上下文检查失败时，不进入包写入；未知状态或权限不足不能当作缺失。

固定目标只有`ghcr.io/xtianxx/payment-hub-migrate`；
初始化标签由程序计算为`init-<40位源码SHA>`，不允许用户传任意包名、角色或tag。
标签只用于定位，实际交付身份仍以原始manifest digest和镜像ID验证。

## 执行与验收

1. 只读解析当前SHA的可信CI候选，按精确run/artifact ID下载原bundle，不重新构建镜像。
2. 验证交接、审计身份及包前提；load后核验两角色原ID、root descriptor、平台和来源label。
3. 紧邻push复查CI及包状态，app原ID和元数据保持不变，migrate仍必须缺失。
4. 仅推送真实迁移镜像的固定初始化标签，不推送或更改app包。
5. 校验push摘要、registry原始manifest字节与响应digest、root和内容身份，
   按digest拉回并核对原镜像ID。
6. 读取并冻结新migrate包的真实ID、owner、仓库关联和可见性；只接受符合授权范围的public或private，
   同时复查app。不根据源码仓库可见性推断包实际状态，不调用可见性修改API。
7. 使用原bundle的本地app与拉回的migrate做配对运行验收。
   这不代表app也已经从registry拉回，不伪造app远端digest或完整配对交付记录。
8. 复查app仍为原包、新migrate身份稳定，并完成凭据清理和独立初始化摘要。

运行验收可能准备PostgreSQL镜像、Go依赖及测试预编译输入，
但不调用镜像builder，不重新跑全部源码业务测试，也不接触正式业务数据库。

`packages: write`是job级权限，不是step级权限。
因此写入job必须独立、步骤最小化、action固定版本且checkout不保留凭据；
显式writer/audit变量只注入所需执行步骤，但不能声称其他action技术上没有job token的包写能力。
审计token只用于GitHub身份及包读取，不用于Docker或registry认证；
Git、Go、验收与Docker子进程不继承两种token，登录只经标准输入传递writer凭据。

## 结果与后续动作

初始化使用独立摘要，不冒充正常`release-summary`：

- 无论初始化是否成功，`published`始终为`false`。
- 只有内容验证、postcheck、运行验收及清理全部成功，才可声明初始化已验证。
- `app`和`migrate`保留冻结身份，`last_observed`记录最后复查阶段及ID/可见性；
  两者不一致时拒绝成功，未知可见性不冒充private或public。
- `process_cleanup_failed`单独记录子进程清理失败；
  `credential_cleanup.cleanup_failed`只记录凭据目录清理失败。
  失败摘要保留原始`failure.stage/kind`，不因后续凭据清理失败覆盖首个错误分类；
  两种清理状态必须一起核对，任一失败都不能作为初始化成功。
- 记录真实可见性：若为private，需要owner后续单独确认并处理公开设置；
  若已经public，不重复更改，也不尝试改回private。
- 初始化成功后仍须另行授权正常release，完成同一源码的两个镜像发布、
  digest拉回及完整配对消费门禁，才能声明完整镜像对已发布。

缺失检查、push及postcheck不是原子事务。
即使初始化验证成功，也不声称能够证明包恰由本次run创建；
存在外部并发写者时，最后一次检查与push之间仍有窗口：
其他写者可能在此时创建迁移包并占用相同初始化标签，后续`docker push`
仍可能覆盖该标签的指向。维护窗口必须排除这些写者；只暂停本工作流不够。
当前未找到GHCR官方支持的标签条件创建、不可变标签或原子预留契约，
不能把额外一次GET、未文档化的`If-None-Match`或事后digest校验当作防覆盖保证。
如果不能确认没有其他写者，不应执行本入口，应先另行评审交付方案。

CI选择复查同样只是时点检查。将它放到包复查之后、紧邻push，
可以拒绝包复查期间已经推进的CI run/attempt，但不能阻止最后一次CI检查之后的新运行。

失败或取消后可能已经残留包、tag或公开内容。
摘要必须保留push是否尝试、已验证或未知副作用以及清理结果；
不得自动重试push、删除包、覆盖未知对象、继续公开化或转入正常发布。
已存在migrate时再次选择初始化会拒绝，应先只读核对前次摘要和远端身份，而不是反复重跑。

## 本轮本地验证（2026-11-16）

- `python3 -B -m unittest discover -s scripts -p 'test_*.py'`：
  exit 0，1181项通过，包含43项初始化fake测试及工作流合同mutation测试。
- 固定Go1.27.4运行`python3 -B scripts/check-ci-contract.py --go <Go1.27.4绝对路径>`：
  exit 0，`ci-contract: pass`。
- 同一Go工具链运行`go test ./tests -run '^TestReleaseGates$' -count=1`：
  exit 0。
- 初始化与正常发布CLI的`--help`及`git diff --check`：exit 0。

本地fake覆盖不能证明远端创建成功。未运行真实Actions初始化、GHCR登录/推送、
可见性修改或真实Docker配对验收。上述验证对应首次实现；
其后已获授权推送分支并创建PR，不代表初始化或部署授权。

## PR #21审查修复验证

本地修复了包复查与最终CI选择的顺序，补强了审计Secret搬移反例及实际runner断言，
并将进程清理失败与凭据目录清理失败分开记录。外部写者覆盖同名标签的竞态
仍是上文明确列出的剩余风险，未宣称已获得GHCR原子创建保证。

- `python3 -B -m unittest discover -s scripts -p 'test_*.py'`：
  exit 0，1188项通过，包含46项初始化测试及140项工作流合同测试。
- 固定Go1.27.4的`check-ci-contract.py`及`TestReleaseGates`：exit 0。
- 初始化CLI的`--help`及`git diff --check`：exit 0。
- 时序与runner断言测试均验证旧实现会失败；审计位置保护禁用时，
  保持Secret总次数不变的搬移反例也会失败。

以上为本地修复验证记录；后续已获授权提交并推送修复，不包含合并、真实初始化或部署。
未回复或关闭远端审查讨论。
PR原head的CI绿灯不作为本地新改动的远端验证证据。

## 官方规则

- [Actions创建包的默认权限与可见性](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions#default-permissions-and-access-settings-for-packages-modified-through-workflows)
- [包可见性与访问控制](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [GHCR容器镜像操作](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [OCI Distribution v1.1.1：推送manifest](https://github.com/opencontainers/distribution-spec/blob/v1.1.1/spec.md#pushing-manifests)

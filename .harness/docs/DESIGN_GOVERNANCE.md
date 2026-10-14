# 需求选择、设计审核与发布

> 统一定义 USER_STORIES / Pick、PRD、UI 与前后端技术方案的人工门禁、修订与架构发布。内容标准分别见 PRODUCT_SENSE、DESIGN、TECH_BACKEND 和 TECH_FRONTEND；这里不重复模板。

## 策略与职责

代码说明实际业务和技术行为，需求和设计说明应有业务和技术行为。对于需求选择、PRD、UI与前后端技术方案产出，Agent自动校验结构、版本和准入，人工评审最终质量。

## 人工门禁与状态

| 对象 | 人工决定 | 阻止的依赖任务 |
|---|---|---|
| USER_STORIES / Pick | 当前需求内容及本轮精确 US/AC 集合 | PRD 及其下游 |
| PRD | 范围、行为、业务/数据流、约束与影响 | UI 与技术方案 |
| UI | 文档、原型/设计稿及影响呈现的本地依赖 | 对应前端方案与实现 |
| 后端技术方案 | 当前方案与架构修改 | 对应后端及共享实现 |
| 前端技术方案 | 当前方案与架构修改 | 对应前端及共享实现 |

适用性由已确认范围与影响面决定，纯后端需求不需要前端技术方案和 UI 评审。没有人工回复、拒绝或批准版本不匹配均保持阻塞；既有有效批准不重复索取。

`Draft → Agent Full Review PASS → 等待人工审核 → 人工批准且发布成功 → Ready → 全部相关实现及架构核对通过 → Runtime 标记 Done`

设计产物发布为 Ready 后，设计任务即可完成；下游 Coding 使用有效的 Ready/Done 版本。全部相关实现完成且 Review 签认 `design_conformance` 后，由前台执行 `task-approve --complete-from`，Runtime 统一更新文档、索引和完成凭证；Coding 不自行修改设计状态。具体核对要求见下方“设计变更与完成核对”。

## 运行入口

1. 编排者整理 Story 主流程与可理解 AC，写入规划的精确 source_stories，执行 `harness requirements select <plan>` 取得当前内容/选择请求；展示具体内容、范围和请求摘要。
2. Codex 用户按请求的 reply 回复 `批准 <request_sha256>` 或 `拒绝 <request_sha256>`，前台执行 `harness requirements select <plan> --from-host` 读取原始用户记录。其他已接入宿主执行 `harness requirements select <plan> --event <event.json>` 后，approved 才确认内容与选择。
3. 设计 Exec 仅提交 Draft。每个 Sprint 的后端、前端技术方案各只有一份，变更修订原文件。索引保留旧行，为当前 Task/Run 新增 draft Entry；涉及已有 Scope Key 时，通过 `Supersedes` 关联被替代的 Entry。发布成功后由 Runtime 将被替代条目标记为 stale，旧正文由发布快照保留。
4. Review 核验领域职责、规则归属、抽象、模型/流程一致性与累计复杂度；运行 `task-review` 后读取其 approval 请求，将当前产物、变化和取舍提交人工。UI Review 使用原型资源目录（如 `["docs/design-docs/sprint-1-prototype"]`）。
5. 技术方案需要修改 `ARCHITECTURE.md` 时，在 `task-context.architecture.candidate_path` 准备候选；候选标题前必须声明 `architecture_implementation_status: pending`，并说明尚未实现的范围。使用 `task-review --architecture-candidate <path>` 一并审查，不得提前修改有效架构。全部实现及整体架构核对通过后，Runtime 将该状态更新为 done；该受管状态值不影响内容摘要。
6. Codex 收到人工审核结果回复后执行 `harness task-approve <task-type> <plan> --task-id <ID> --from-host`；签署事件宿主执行 `harness task-approve <task-type> <plan> --task-id <ID> --event <event.json>`。Runtime 校验真实决定和当前内容，统一发布文档/索引/架构/凭证。
7. 发布失败保持阻塞，恢复只允许已知的事务前后内容；遇到外部修改先解决冲突。架构基线被另一方案更新时，必须合并并重审，不能覆盖。

架构候选的 Review JSON 同时声明 architecture_impact：affected_design_task_ids 列出被改变共享决定的其他设计任务，rationale 说明影响判定依据（空集合也要说明）。它随候选共同获人工批准；这些任务不能沿批准架构链复用旧结论，必须回到当前方案重审。其他任务允许沿已批准的架构版本正常推进。task-context.architecture 的 path/sha256 始终指向当前可读取且获准使用的架构，baseline_sha256 保留轮次建立时的基线；所有实际绑定架构的设计和 Coding 任务均返回这一输入。候选 Review 绑定审核时的有效架构基线，发布前再次检查，防止覆盖并发变化。

## 文档身份、索引与摘要

正文标题前声明唯一 `document_status: draft|ready|done`；前后端方案另声明 `technical_design_contract_version: 2`。Runtime 仅将该受管状态值排除出内容摘要，其他正文和依赖均受版本约束。手写 Ready 不形成有效发布。使用 `harness task-approve --digest <file>` 获取正文内容摘要。
同 Sprint 同类技术方案只有一个当前文件；不同 Sprint 使用不同文件。历史索引行通过发布快照解释原内容，当前版本通过唯一有效 Entry 检索。同文件修订发布完整作用域集合，删除的作用域自动标记 retired 并退出当前集合，保留旧正文快照；重新引入时作为新版本登记。显式迁移前可把原多份方案归并到一个当前文件，原文件保留 stale 历史关联。verified 只兼容历史，不能代表新版人工批准。
PRD 正文使用七列表，索引文件链接以 `#SCOPE-001` 关联范围，Scope Key 保留跨迭代模块语义；技术方案正文不复制索引列或作用域清单。

## 设计变更与完成核对

- 业务目标、范围或用户可见结果变更回到 Story/Pick/PRD；交互变更回到当前 UI；模型、接口、职责或机制变更回到当前技术方案，必要时更新架构。仅实现缺陷修代码，新诊断样本留在已有测试/运行记录。
- 使用既有 scope_conflict / task-reopen 路线重审受影响决定及依赖；同责任域多个方案时由 Review 的 responsible_task_ids 定位任务。未受影响的批准保留，不机械把所有反馈回写需求。同一责任域有多个候选任务时，每条 finding 都必须给出 responsible_task_ids，缺少定位则阻断回退并要求明确 Review。同一 Review 的多个责任域合并其依赖回退集合；缺少的责任任务逐域补齐，任何跨 Sprint 路线都会转移完整 Review，不能只处理最早责任域。
- 审查当前合并设计，特别关注重复规则、状态多处推导及新增协调/恢复机制。设计缺陷可用职责冲突或矛盾定义证明，无须先制造运行故障。
- 完成 Review 对照“架构约束 → 代码落点 → 验证”及“实际结构/机制 → 架构归属”，通过已有 Review 证据完成核对后才允许文档 Done。架构仍为长期唯一真源，历史 Sprint 文档只记录当轮变化背景。
- 实现/质量 Review 的 design_conformance 包含 design_run_ids、当前 architecture_sha256，以及 observations（object、implementation、verification；后两者必须是该 Review 实际绑定文件）。完成所有相关实现后，执行 `task-approve <type> <plan> --task-id <ID> --complete-from <已核对任务 ID>` 一致更新 Done；人工改状态不算完成。Done 凭证同时绑定全部相关实现的当前轮次、Review 和产物；新增实现、代码变更或重新 Review 均使旧完成依据失效。设计未变时仍可用 Ready 发布作为实现输入，重新执行 --complete-from 更新完成证据，无需重复人工批准设计。
- 同一方案的并行实现无需互相建立依赖；完成核对覆盖其全部已验证实现。架构整体转 Done 还要求核对涵盖本轮全部技术方案和实现。新版 sprint-close 在归档前检查本轮各设计的 Done 核对记录及架构已实现状态，避免已交付代码仍对应待实现架构与 Ready 设计。

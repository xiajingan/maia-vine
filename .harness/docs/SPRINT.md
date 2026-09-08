# Sprint 规范

> Sprint 生命周期、Agent 编排架构、任务流转和发布编排。
>
> **单一真源边界**：
> - `docs/SPRINT.md`：Sprint / deploy-sprint 的流程与编排协议
> - `.harness/rules/task-rules.yml`：任务类型、工具边界、门控、产出物、验收条件、派生规则（机械执行真源）
> - `config/harness.yml`：项目级流程开关（走查环境、质量阈值、部署模式、UI L3）
> - `config/technology.yml`：技术栈、组件 manifest 与必需命令能力
> - `docs/CICD.md`：CI/CD why-only 红线、分支/环境策略、故障速查；不定义 Sprint 流程

**核心原则：Sprint = PR 级别交付单元。** 每个 Sprint 聚焦单一可交付增量，粒度对齐一个 PR。

---

## Sprint 启动协议

先执行 `harness sprint init sprint-N-name --type <feature-sprint|maintenance|library-sprint|hotfix|deploy-sprint-test|deploy-sprint-prod|control>`。Runtime 在创建任何文件前同时校验 `project.mode` 与 `project.type`；`library-sprint` 仅允许 `type: library`，产品/部署 Sprint 只允许 backend/fullstack/frontend。Runtime 从 `delivery.remote/refs` 解析并刷新远端基线；Feature、Maintenance、Library、Hotfix 同时创建 linked worktree，禁止回退到当前 HEAD 或主工作区。计划确认后执行 `harness sprint activate <plan>`，再由第一个任务的 Step 0 触发环境验证。

### 启动检查

1. **执行 `uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-plan-file> --task-id <task-id> --strict`**
2. `sprint_gate.py` 内部自动执行 `uv run --project .harness/runtime harness verify preflight --skip-if-recent <ttl>`
3. 检查结果非 0 时：使用 `ask_user` 工具向用户展示失败项，引导用户修复后重试
4. 第一个任务 gate 通过后，在 Sprint 计划文档标注 `环境就绪: ✅`

### 环境恢复

当 preflight 检查失败时，编排者提供具体的修复指引：

| 失败项 | 修复指引模板 |
|--------|------------|
| Docker 服务未启动 | `docker compose up -d` 后等待 healthy |
| 环境变量缺失 | 列出缺失变量名 → 引导编辑 `.env` 文件 |
| 数据库表不足 | 执行 `ARCHITECTURE.md` 声明的 Python migration 命令 |
| 外部服务不可达 | 列出不可达 URL → 引导检查网络/密钥/白名单 |

> **禁止在环境未就绪时继续执行任务。** Preflight 是 Sprint 的硬性前置条件。

---

## Agent 架构

Sprint 编排者由主 Agent 在前台承担；只在当前任务协议需要时通过 `task` 工具启动子 Agent：

Codex 是默认运行时，角色定义位于 `.codex/agents/*.toml`；Agy/Copilot 是分发兼容层。进入 Sprint 即授权按本协议顺序派生三角色，但不授权无关并行任务。普通单点任务不进入本协议。

| agent_type | 步骤 | 角色 | 职责边界 |
|------------|------|------|---------|
| 主 Agent（前台） | Step 0 | Sprint 编排者 | 规划迭代、运行 Pre-Flight、调度子 Agent、管理任务状态；禁止委托给 `harness-sprint` 子 Agent |
| `harness-plan` | Step 1 | 规划者 | **自主**加载 task-rules.yml + 任务规范 → Readback → 生成执行计划；接收 Review 反馈重新规划 |
| `harness-exec` | Step 2 | 执行者 | **自主**接收执行计划，独立决定实现细节，产出任务交付物 |
| `harness-review` | Step 3 | 审查者 | **自主**对照规范验收条件审查产出物（PASS/FAIL）；只读不写 |

> **关键原则**：每个 `task × attempt × role` 都使用独立的新 Agent 实例。编排者只传递上下文边界（见下方调度协议），不传递实现细节，也不通过旧会话记忆交接。

---

## 任务执行协议

每个任务都经过 Preflight 与 Review Gate；中间执行链由 `task-context` 返回的唯一协议决定：

```
主 Agent / Sprint 编排者（前台）
  └─ 逐任务执行：
     ├─ Step 0: PRE-FLIGHT       ← 主 Agent 自行执行
     ├─ agent: PLAN → [entry Action] → EXEC → harness-plan → 前台 task-action → harness-exec
     ├─ action: EXEC             → task-action（不派生 Plan/Exec Agent）
     ├─ orchestrator: EXEC       → 前台按规则 steps 执行
     └─ REVIEW                   → agent-full 派生 harness-review；artifact-only 使用确定性证据
         ├─ PASS → 执行 `sprint-gate ... --task-id <task-id> --phase review --strict` → 提交修改
         └─ FAIL → 回到 Step 1 重新规划（默认最多重试 2 次）
```

### Sprint 交付契约

Sprint 不是技术任务容器，而是一次可被验收的产品/能力增量；`sprint init` 生成的计划必须在激活前填写以下机器可读字段：

| 字段 | 约束 |
|---|---|
| `planning_contract_version` | 新 Sprint 固定为 `3`；`1/2` 仅兼容升级前已激活计划 |
| `requirement_mode` | `stories` 表示产品需求来自 USER_STORIES；`non-product-change` 表示没有产品行为变化，允许值按 Sprint 类型由 `task-rules.yml#sprint_requirement_modes` 决定 |
| `source_stories` | `requirement_mode=stories` 时必填 `[{id: US-001, acs: [US-001-AC-01]}]`；AC 是 Acceptance Criterion（验收条件）。active 阶段校验 `ready` Story、场景内核、约束和 AC，并将选中内容摘要与 `.harness/state/requirements/confirmations/` 中的版本化 Boss 确认绑定激活状态及每个 attempt；`sprint-close` 归档后，全部当前 AC 已交付的 Story 为 `done`，部分 AC 交付则保持 `ready` 并由 completion receipt 绑定本轮 AC 语义；生命周期变化不改变需求内容摘要 |
| `impact_surfaces` | Feature 必填 `{影响面: "具体原因"}`；可用值与其必需任务/facets 只来自 `task-rules.yml#impact_surface_requirements` |
| `delivery_strategy` | `domain-bootstrap`、`vertical-slice`、`consumer-capability`、`deployment`、`refactor`、`remediation` 或 `governance` |
| `observable_outcomes` | 至少一种可观察结果：用户交互、业务流程、对外接口、数据模型、消费者契约、行为保持、部署/运维或治理结果 |
| `boss_observation` | `{entrypoint, action}` 对象，分别声明 Boss 使用入口和实际操作 |
| `outcome_acceptance` | 非空数组，逐项声明该入口上可以明确判断 PASS/FAIL 的结果 |
| `domain_coverage` | `{领域: [任务 ID]}` 对象；领域必须存在于 `ARCHITECTURE.md`，任务必须声明具体产出物和验收条件 |

策略、可观察结果兼容关系和各 Sprint 类型的终态任务统一声明在 `task-rules.yml#delivery_strategies/sprint_delivery_contracts`，Runtime 对规则本身及计划引用执行 fail-closed 校验，文档不重复维护枚举矩阵。

### 需求接入与反馈同步

USER_STORIES 是需求唯一输入真源，PRD、技术方案、测试和验收都是派生产物。用户直接提出新需求时，编排者必须先把原话和已确认事实整理为临时 Story input，执行 `harness requirements intake --input <path>` 写入 `draft`；未知字段保留 TODO。向 Boss 回读场景、约束、非目标和 AC 并确认后，使用 `harness requirements confirm <Story ID...> --by <name>` 原地转为 `ready` 并记录逐 Story 内容摘要。已有 ready Story 内容变化时同一命令执行重新确认；activate/amend 同时绑定需求摘要与 ask_user 确认凭证，手工改状态不能旁路。

一轮 Sprint 可以点选一个或多个 Story，并为每个 Story 选择一个或多个 AC。用户未指定 Story 时，编排器只展示 ID、标题和状态供用户点选，不得默认纳入全部需求。启动规划时若被选 Story 为 `draft` 或 ready 但缺少当前内容确认凭证，编排器先批量回读并等待 Boss 确认，再执行 `harness requirements confirm <Story ID...> --by <name>`；该命令不重写需求语义。未被本轮选中的 Draft 不要求确认，也不能进入 `source_stories`。

Story input 使用唯一的紧凑 YAML 契约：顶层为 `id/title/priority/scene/statement/constraints/acceptance`；`scene` 使用 `user/context/trigger/current_difficulty/observable_outcome/source`，`constraints` 使用 `business_rules/external_constraints/non_goals/assumptions`，每个 acceptance 使用 `id/given/when/then`。该文件只放在 `.harness/runs/requirements/` 作为命令输入，不是第二份需求真源；成功同步后以 USER_STORIES 为准。

Boss 走查未通过时，`acceptance-record reject` 必须显式声明反馈分类：实现缺陷和非产品问题回退 code；需求澄清/变化必须更新当前 Story；新增行为必须创建新 Story 并加入当前 `source_stories`。后两类按当前 Sprint 的 Story owner route 回退并要求 `sprint amend` 重新绑定需求与确认摘要，旧 attempt 自动失效。当前结果通过但提出后续优化时，使用 backlog disposition 创建后续 Story，不改变当前 Sprint 输入；已完成 Story 不得原地改写。

`planning.project_stage=greenfield` 时，所有 Feature/Library Sprint 必须持续使用 `domain-bootstrap`，先广度优先建立领域边界、目录骨架、数据所有权、Port/公开契约和最小 walking skeleton。整体领域框架完成后，以已通过且覆盖全部领域的 Sprint outcome 执行 `harness sprint bootstrap-complete --evidence <sprint-outcome.yml>` 生成只读候选；项目负责人核对候选摘要后，在主交互中提供含 `decision: approved`、`source: ask_user`、`confirmed_by`、`confirmed_at` 和 `candidate_sha256` 的外部 signoff，再执行 `harness sprint bootstrap-approve --signoff <path>`。只有签署路径、outcome 内容摘要和 ARCHITECTURE.md 摘要同时有效时才可显式切换为 `established`；缺少阶段字段一律按 greenfield fail-closed。Feature bootstrap 的每个领域必须映射领域设计与 code，且全局包含 infra；Library bootstrap 的每个领域必须映射 library-design 与 library-code。`migrate` 仅把非空且未使用 v2/v3 planning contract 的旧 completed Sprint，或 schema v1 的历史激活状态，作为老项目已经进入交付阶段的证据并迁移为 `established`；架构文档、目录或业务代码本身不用于推断项目阶段，也不会伪造 bootstrap receipt。

每轮规划必须从 Boss 可观察结果反推 PRD、设计、实现、测试和产品走查。重构可以独立成为一轮迭代，但必须声明可实际走查的接口、数据模型、业务流程或行为保持结果，并给出明确 PASS/FAIL；“代码更整洁”“测试更多”或“基础设施更完善”本身不是可验收结果。

走查指南必须逐项包含交付契约的入口、操作和预期结果。`task-review` 型终态 Review 必须在 Review JSON 的 `outcome` 中亲自签认 planning contract 摘要，以及逐条 `acceptance/path/sha256/observed/result` 结构化观察，并把每个 path 登记为当前 attempt 的 Review artifact；记录 Review 后执行 `harness sprint outcome <plan> --task-id <terminal-task-id> --evidence <actual-result-file>` 只生成对应公共 receipt，不能自行产生 PASS。Feature/Maintenance 的 acceptance report 必须在“Boss 走查记录”下提供 `验收条件 | 实际观察 | 结果` 表，approve 只签认该表，不会从 expected 自动合成 observed。所有终态都会拒绝缺失、内容漂移、事后补写、symlink 越界、非当前 attempt artifact 或来源错误的证据。

### Step 0: PRE-FLIGHT（编排者自行执行）

每个任务开始前按顺序执行：

1. **执行脚本门禁**：`uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-plan-file> --task-id <task-id> --strict`
2. **基础设施就绪**：若任务类型定义了 `infra_action`，通过 Action Executor 执行；非 0 退出码时使用 `ask_user` 协调修复
3. **用户确认就绪**：需要人工操作的门控保持阻塞，直到收到明确确认

`sprint_gate.py` 的默认 `preflight` 阶段负责校验依赖状态、上游产出物、质量报告和 L3 审批记录；不会提前执行 `artifact_action`。v3 计划的 `依赖` 列只接受任务 ID，Runtime 校验不存在未知、逆向或循环依赖，并要求每个任务的依赖闭包到达最近的实际前序阶段；阶段规则控制公共执行顺序，显式依赖闭包逐 ID 要求 done 且有当前 PASS attempt。Sprint Gate 始终验证完整依赖闭包；`task-context.upstream_inputs` 再按 `task_input_projections` 投影当前任务的直接产物输入：当 Coding 的闭包存在适用技术方案时，只暴露该技术方案，不再并列暴露 PRD/设计，但其上游漂移仍会经 Gate 阻断。没有技术方案任务的显式 maintenance/hotfix 流程保留最近依赖，不虚构方案。

### Step 1: PLAN → 启动子 Agent (agent_type: harness-plan)

仅适用于 `execution_protocol=agent`；Action 和 orchestrator 任务跳过本步骤。

子 Agent **自主完成**以下工作（编排者不代劳）：

1. 从 `.harness/rules/task-rules.yml` 获取任务类型对应的规范路径，**加载规范原文**
2. 只按 `task-context.upstream_inputs` 中绑定的精确前序 run、Review report 和 artifact 加载上游产出物；存在适用技术方案时，Coding 只获得技术方案投影，PRD/设计仅供技术方案任务消费；文档任务先通过作用域注册表定位 current 条目，不得用整个目录或未登记历史文档替代
3. 执行 Readback（输出执行步骤和评分标准原文摘录），Readback 与规范不一致则禁止继续
4. 生成详细执行计划（修改哪些文件、实现方式、验收标准）
5. 执行计划只写入 `harness task-context` 返回的 `.harness/runs/.../plan.md`；`docs/exec-plans/` 只保存 Sprint 级计划

### Step 2: EXEC → 启动子 Agent (agent_type: harness-exec)

`agent` 任务由子 Agent 接收计划全文并实施；若声明 `entry_action`，前台必须在 Plan 已绑定且计划文件落盘后、启动 Exec Agent 前运行一次，Runtime 拒绝缺 Plan、并发、覆盖或同 attempt 重跑。`action` 任务只运行声明的 `task-action`；`orchestrator` 任务由前台执行规则 steps。三种主协议互斥，agent 内的 entry Action 只是受控副作用子阶段，不是第二条执行协议。

`pr` / `library-pr` 的 preflight 身份绑定允许当前 Exec Agent 创建提交，但不自动信任任意 HEAD 漂移。Exec 每创建一个提交后必须立即按 `task-context.git_identity.advance_command` 调用 `task-commit`：Runtime 仅接受当前 attempt 已绑定的 Exec invocation、同一分支且以已登记 HEAD 为唯一父提交的直接后继。跳过中间提交、merge、未登记提交、切换分支、rebase/reset/force-push 造成的历史改写都会使后续 Review 阻断，但逐个登记的合法提交不会消耗 retry 或创建新 attempt。

### Step 3: REVIEW → 启动子 Agent (agent_type: harness-review)

`agent-full` 派生子 Agent 审查产出物；`artifact-only` 由前台根据确定性 Action/Gate 证据填充同一 JSON 契约，不派生子 Agent：
- **PASS** → 将 `scope=full` 的 Review JSON 写入 `task-context.review_report`；完整覆盖并通过全部稳定验收 ID 后，才可通过 `task-review` 登记并执行 Review Gate
- **FAIL** → 存在范围内、可复现的 defect/regression；生成具体问题 + 修复建议 → 创建新 attempt，并按该任务原执行协议重试
- **INCOMPLETE** → 只有证据、环境或范围缺口；先分诊补证、修复环境、拆分任务或请求裁决，不得伪装成 Major defect

`focused` Review 只能用于定位修复项，不能产生最终 PASS。阻断 finding 必须包含稳定 finding key、验收 ID、严重级别、可复现证据、不变式、场景、可观察故障、质量属性影响和修复建议；猜测不得作为 blocking finding。

Review 若发现输入层级错误，不在当前任务内静默改上游，也不按普通缺陷重试。`scope_conflict` finding 必须声明 `responsible_scope: story|product|design|technical-design` 并以 INCOMPLETE 登记；前台随后执行 `harness task-reopen <sprint-plan> --from-task-id <task-id> --reason <reason>`。Runtime 按 `task-rules.yml.scope_conflict_routes` 选择当前 Sprint 类型的责任任务并传递性回退；不属于当前流程的责任域生成 `transfer-required` receipt，当前 Sprint 持续阻塞，必须转入配置的新 Sprint。目标计划必须声明 `scope_transfer_from: <源 Sprint>/<源 Task>/<源 Run>`，该来源同时进入目标 planning contract 摘要；目标 Sprint 归档且 canonical outcome 通过后，用原命令追加 `--transfer-sprint <目标 Sprint ID>`，Runtime 校验类型、来源、终态 producer、planning contract、计划与 outcome 摘要后才解除门禁。Story 回退必须存在受影响 Story 的 ask_user feedback、重新确认和 amend；缺失责任任务时 amend 必须实际新增配置的 owner task，任意文档或结构变化都不能解除门禁。

### 文档作用域注册表

`docs/product-specs/index.md`、`docs/design-docs/index.md` 与 `docs/tech-docs/index.md` 是项目自有的机器可读注册表。每行绑定 Entry ID、Scope Key、文档 SHA-256、Sprint、Task ID、Run ID、来源和实际模块/页面/表/API/组件；同一 Scope Key 最多一个 `verified` current。Exec 只登记当前 attempt 的 `draft`，且只能引用本轮精确选中的 Story/AC（non-product-change 只能引用 ARCHITECTURE/ASSIGNMENT），正文作用域清单必须与本 attempt 索引逐行一致；Review PASS 后 Runtime 才发布这些行并将其 Supersedes 目标标为 `stale`，同时把该 attempt 的不可变 publication receipt 纳入 Review artifact，后续任务不依赖会继续变化的共享 index 摘要。历史文件不得跨 Sprint 复用或改写，`doc-lint --ci` 校验完整注册表。旧版索引必须显式执行 `harness registry-migrate`；缺少 Scope Key、页面/API/组件语义的旧简单索引必须通过工程内 `--mapping <yaml>` 逐文件确认，禁止自动猜测，可用 `--rollback` 在索引未被继续修改时恢复。

旧简单索引 mapping 固定为 `version: 1` 与 `entries`；每项填写 `directory/file/entry_id/scope_key/module/source/sprint/status/supersedes`，产品/设计再填 `page_or_area`，技术方案再填 `tech_kind/table_or_api/component`。迁移生成的 receipt 会绑定 mapping 路径与 SHA-256；字段缺失、多余、未匹配或语义不合法时原索引保持不变。

### 产出物模板约束

当规范定义了评分体系（如 `QUALITY_SCORE.md`）时，报告维度和权重 **必须与规范一致**，Agent 只填写实际得分和证据，禁止自定义维度或修改权重。

---

## 子 Agent 调度协议

> **核心原则：编排者只传递上下文边界，子 Agent 自主完成工作。**

编排者先从 `task-context.agent_invocations` 获取绑定当前 attempt 的 `instance_name` 和 `invocation_id`，再启动子 Agent。每个实例只允许调用一次；Codex 不得使用 `followup_task` 复用旧 Agent，Agy 不得重复调用同一动态实例，其他运行时也必须创建新会话。子 Agent 启动后使用对应 role 和 invocation ID 再次调用 `task-context`，将唯一声明写入当前 attempt；attempt 归档时该记录随证据一并保留。

### 编排者传递内容（输入边界）

| 子 Agent | 编排者传递 | 编排者禁止 |
|----------|-----------|-----------|
| harness-plan | 任务 ID + 类型 + Sprint 计划路径 + 上游产出物路径列表 + task-context.plan/open_findings/planning_advisories | ❌ 预写执行计划内容 |
| harness-exec | Step 1 生成的执行计划**原文** | ❌ 预写代码/文档内容、补充额外实现指令 |
| harness-review | Step 2 产出物路径 + 规范 + 稳定验收 ID + task-context.review_report | ❌ 预判审查结论 |

### 调度约束

1. **全新实例**：Plan、Exec、Review 分别创建新 Agent，任何实例不得处理第二个 role、task 或 attempt
2. **实例绑定**：prompt 必须原样携带 `invocation_id`；子 Agent 通过 `task-context --role ... --agent-invocation-id ... --agent-runtime ...` 绑定后才能工作
3. **Review 独立**：Review Agent 不得参与本轮 Plan/Exec，也不得复用上一轮 Review 上下文
4. **禁止越权**：编排者不得在 prompt 中预写实现代码、文档内容或设计方案
5. **原文传递**：Step 1 → Step 2 传递执行计划原文，编排者不得改写或补充
6. **独立执行**：每个子 Agent 自主加载所需规范文件，编排者不代为加载后粘贴
7. **顺序阻塞**：Step 1 完成后才启动 Step 2，Step 2 完成后才启动 Step 3
8. **失败重试**：Step 3 FAIL 后创建新 attempt，并用新的三角色实例重新执行；旧 Agent 不得接收 follow-up
9. **轮次隔离**：FAIL 后使用 `sprint-gate ... --new-attempt --increment-retry` 创建新轮次；旧轮次 Agent、Action 和 Review 证据不得复用

`instance_name`/`invocation_id` 由 attempt 的随机 `run_id` 派生，因此不同任务轮次和角色不会冲突。Runtime 能验证声明的 invocation 唯一且绑定当前轮次，但宿主未提供可信 session identity 时，是否真正创建了新上下文仍由上述调度协议保证。

### FAIL 重试协议

REVIEW FAIL 时，任务进入重试循环（默认最多重试 2 次，不含初始轮次），始终重新执行该任务完整的既定协议和 Review Gate。remediation 任务与父任务共享重试预算；人工重置必须提供 `--reset-retry-reason`：

1. **Step 1 PLAN**：传入 Review 反馈原文 + finding ledger + 原执行计划路径，harness-plan 先做根因和范围裁决，再生成 closure matrix 与修复计划
2. **Step 2 EXEC**：harness-exec 按修复计划执行
3. **Step 3 REVIEW**：harness-review 先 focused 验证既有 finding 与固定回归；未关闭则直接 FAIL，全部关闭后才按任务原始适用验收执行最终 Full Review

**Full Review 完整、深度检查有边界**：最终结论始终覆盖全部适用验收 ID；深度检查以本轮 diff、直接影响面和明确不变式为边界。remediation 不会自动成为新验收标准。

### Task facets 与规模提示

任务表可选增加 `facets` 列（英文逗号分隔），从 task-rules 对应任务类型声明的 facets 中选择。Runtime 只合并适用 facet 的验收条件；未声明时使用任务默认 facets。设计任务的条件专项默认不启用，只有 PRD/Architecture 的触发器成立时才声明对应 facet；不能用 project.type 推断性能、安全、并发等风险。`task-context.planning_advisories` 只提示拆分风险，不改变重试策略或自动阻断。

新建 `integration` 任务必须显式填写 `facets`。只读验证不声明 producer；任何 producer 都自动进入 `controlled-write` 合同，并要求任务表包含 `producer=command:<config.commands 名称>`、`checkpoint` 和 `failure_strategy=resume|fix-forward|restore-checkpoint`。`schema` producer 必须与 `integration.migration` 的 `checkpoint-one-way` 命令一致。Preflight 后先完成 Plan Agent，再由前台执行一次 `task-context.entry_action.command`；该命令绑定当前 attempt，在受控超时内执行 producer，并以 run-scoped 状态固化运行中、超时/失败或成功结果、交付身份和工作区快照，随后 Exec Agent 只补齐 `docs/test-reports/integration/<task-id>/receipt.yml` 中的 operations（目标与作用范围）、带摘要和 SHA-256 的 before/after/checkpoint/health evidence、失败记录及最终结果。Review Gate 会校验执行合同、当前 `promote-test` attempt 回执、producer attempt、最终 health evidence、Test 部署身份以及源码和不可变制品未变化；工作区保护只排除当前任务目录，不能修改其他 integration 任务证据。失败或 abandoned producer 禁止自动重跑：新 attempt 必须在 Sprint 行用 `recovery_of=<上一次 run_id>` 显式绑定恢复来源，并复用 checkpoint 与既定失败策略；已成功的写 producer 不得重复执行。

### 质量回退协议

`quality` 任务不达标（< 95 分）时：

1. `quality` 任务状态标记为 `blocked`
2. 对应 `code` 任务状态从 `done` 回退为 `in-progress`
3. 重新执行 `code` 任务的 Step 1 → Step 2 → Step 3，质量报告作为 Plan 输入
4. `code` 任务 REVIEW 通过后，重新执行 `quality` 任务

---

## 迭代生命周期

**状态**：`planning` → `active` → `completed`

**流程**：`harness sprint init` → 确认计划 → `harness sprint activate` → 按依赖执行 → `sprint-close` 将计划归档到 `completed/` → `pr` 合并并安全清理 worktree

**任务状态**：`pending` | `in-progress` | `done` | `blocked` | `spawned`

**Sprint 闭环协议**（L3 走查通过后，编排者依序执行）：

1. `sprint-close` 在 sprint 分支中归档计划：`docs/exec-plans/active/` → `docs/exec-plans/completed/`，并更新 `AGENTS.md`、执行 `harness requirements complete <sprint-id>`；Runtime 按 AC 当前语义累计已签收证据，全部 AC 已交付才把 Story 从 `ready` 更新为 `done`，部分交付保持 `ready` 并生成可随分支合并的 `docs/acceptance-reports/<sprint>-requirements-completion.json`，且校验 approved Boss signoff、planning contract 与需求摘要不变
2. `pr` 把上述归档与交付物一起合入目标远端分支；禁止先合并、后在已删除 worktree 中寻找计划
3. 明确确认合并后，从主工作区执行 `harness worktree destroy <sprint-id> --merged-into <remote-ref>`；dirty、未合并或无法验证时拒绝删除
4. 只有人工恢复场景可使用 `harness worktree recover-destroy <sprint-id>`，该命令会明确报告强制删除目标

> 归档先于合并，清理后于合并，这是保证 completed 计划进入目标分支且 worktree 可恢复的固定顺序。

**迭代完成校验**（闭环前逐项确认）：
1. 全部任务状态为 `done`（无 `pending`/`blocked`/`in-progress`）
2. L3 门控任务已获用户通过 `ask_user` 工具明确确认（`通过`/`approved`），且已生成 `sprint-N-boss-signoff.yml`
3. `config/technology.yml` 对应必需命令全量通过 + 质量评分 ≥ 95 + 产品走查通过
4. 产出物文件**物理存在**：`docs/acceptance-reports/sprint-N-acceptance.md` + `sprint-N-walkthrough.md` + `sprint-N-boss-signoff.yml`

### 手动 PR 工作流

当 Git 托管平台无 CLI API（如 GitLab 无 `glab`）时，`pr` 任务仍必须通过 `harness pr-adapter create` 执行；GitLab push-options fallback 只允许封装在 adapter 内部，禁止 Agent 在任务中直接拼接 MR 命令。

1. **Agent 执行全量检查**：运行 `command_groups.precommit` 和项目构建命令作为 CI 替代
2. **Agent 调用 adapter**：`uv run --project .harness/runtime harness pr-adapter create --base <target> --head <source> --title <title> [--body-file <file>]`
3. **adapter 创建 MR 或输出阻塞原因**：有 `glab` 时走 GitLab API；无 `glab` 时由 adapter 内部使用 GitLab push-options 创建 MR
4. PR/MR 合并由用户手动操作，Agent 使用 `ask_user` 确认合并完成

---

## Sprint 阶段与任务流

> 任务类型明细、门控、工具、产出物、验收条件与派生规则只在 `.harness/rules/task-rules.yml` 定义；本文只说明编排者如何选择流程。

### Feature Sprint

Feature Sprint 是 PR 级交付单元，默认创建 worktree。含 `code` 的 Sprint 必须包含 `product-acceptance`、`pr`、`sprint-close`。

| `config/harness.yml.walkthrough_env` | 任务流 |
|---|---|
| `development` | `product` → `适用的 design/backend-design/frontend-design` → `code` → `test-case-gen` → `quality` → `product-acceptance(L3)` → `sprint-close` → `pr` |
| `test` | `product` → `适用的 design/backend-design/frontend-design` → `code` → `test-case-gen` → `build-image` → `promote-test` → `integration` → `quality` → `product-acceptance(L3)` → `sprint-close` → `pr(develop + test)` |

`test` 模式表示质量评分和产品走查必须基于 Test 环境；因此 `build-image`、`promote-test`、`integration` 必须在 `quality` 前通过。`promote-test` Preflight 自动检查部署配置、密钥和目标环境，再通过项目部署 adapter 执行真实 Test deploy 并写入 deploy state，不以 `promote/*` MR 代替环境交付；部署后、Review 前显式生成当前 run 专属回执，Review Gate 只读校验其绑定的 build state、每个 artifact/digest 和本轮 deploy state。紧随其后的 `integration` 只消费该回执，按任务 facets 完成 Mock-free 的真实环境处理与验证，不能反向成为 `promote-test` 的前置，也不能修改代码或镜像。Boss 走查通过并完成 `sprint-close` 归档后，`pr` 任务必须完成两条 MR：Sprint 分支 → `develop`，已走查 commit_sha → `test`。`pr` Review Gate 强制校验该 SHA 已同时抵达两个远端 ref；未抵达时禁止清理 worktree。

### Deploy Sprint

Deploy Sprint 只处理发布任务，不创建 worktree，不承载产品/代码设计任务。

Test Deploy Sprint 的 `base_sha` 就是唯一部署候选 commit；所有 `source_sprints` 的 Boss signoff commit 必须已被该 commit 纳入，执行 `build-image` 时当前 `HEAD` 必须与其完全一致。构建状态记录完整 commit 和 signoff 摘要，后续 `promote-test` 会再次校验，不能用另一个工作区状态或未审批提交替代。

| 目标 | 任务流 |
|---|---|
| test | `build-image` → `promote-test`（自动 Preflight）→ `integration` |
| prod | test 链路通过后 → `release-prep` → `migration-design` + `regression` → `release-approval(L3)` → `prod-deploy` → `back-merge` → `observe` |

生产业务升级与数据迁移按长任务处理：`prod-deploy` 开始时固定 candidate，按批次提交 checkpoint；迁移、质量检查、服务启动或发布失败均保留业务候选、候选数据和已提交进度，修复后从最近 checkpoint 继续。框架不得主动回滚业务版本、执行数据 down、重建候选或倒退迁移进度；只有人员在当前主交互中明确要求发布回滚并生成 authorization receipt 后，才能执行 `release-rollback`。完整契约见 `MIGRATION.md`。

### Control Sprint

Control 仍使用统一 Sprint 编排，不建立独立命令式工作流。用户入口是 Control 工程的 `USER_STORIES.md` 和“规划/执行 Control Sprint”的 Chat 意图；`harness-control` 选择任务，单个任务继续由 `harness-task` 执行 Preflight → Plan → Exec → Review。

| 阶段 | 任务流 |
|---|---|
| 需求分发 | `managed-project-check` → `assignment-dispatch` |
| 纳入跟踪 | `assignment-status`；只在用户再次主动启动 Control Sprint 时读取 |
| 系统集成 | `delivery-verify` → `release-compose` → `test-deploy` → `test-integration` |
| 发布 | `release-promote`；失败时保留候选并固化 `integration-finding`；仅人员明确要求时规划 `release-rollback` |

`assignment-dispatch` 从 Control `USER_STORIES.md` 的 Story 派生 `product` 或 `architecture` Assignment，并以 `source_reference` 保留追溯；消费工程也可从本地 Story/Task 派生 `dependency` Assignment。用户无需先手写 JSON 或直接运行 CLI。CLI 是 Skill/Agent 在 Step 0/2 调用的稳定执行端口，CI/Lint 是验证门禁，Commit/PR 是任务产物，均不是与 Sprint 并列的入口。Assignment 只写需求输入，不创建目标工程代码任务，也不修改其 Sprint 状态。

### Library Sprint 与同步依赖任务

`project.type: library` 的工程仍使用 Harness，但只运行公共 API、兼容性、包质量、不可变交付和 PR 闭环，不承担产品走查与环境部署。任务顺序的机械真源是 `task-rules.yml.library-sprint`。

消费工程 Sprint 需要同步修改公共包时，必须先在技术方案中把能力归属到已登记 Provider，再增加 `dependency-change` 任务。该任务通过 `harness dependency` 启动 Provider 的独立 Library Sprint、接收 Build Once candidate、执行消费方契约、验证 Delivery 和供应链 receipt，并检查消费工程锁文件的精确版本与 SHA-256。Review Gate 只接受状态为 `completed` 且绑定当前 consumer task 的唯一 session。异步需求仍走 Assignment，两条路线不得为同一请求重复启动。

### Hotfix

Hotfix 是线上事故专项 Sprint，允许创建 hotfix worktree，但仍复用任务类型和 Step 0→3 协议；生产合并与回灌走 `prod-deploy` / `back-merge` 任务，不恢复旧 `harness-release` Agent。

### task-rules.yml 字段边界

| 字段 | 含义 | 执行时机 |
|------|------|---------|
| `entry_action` / `execute.action` | Python Action Registry 中的稳定任务动作 | Step 2 EXEC |
| `infra_action` | Python Action Registry 中的任务级基础设施检查 | Step 0 PRE-FLIGHT |
| `prerequisites` | 必须全部满足的前置条件 | Step 0 PRE-FLIGHT |
| `prerequisites_any` | 每组满足任意一个即可的前置条件（如 inline test 部署或独立 deploy-sprint） | Step 0 PRE-FLIGHT |
| `artifact_action` | Python Action Registry 中的产物门禁 | Step 3 REVIEW / 闭环 |
| `external_evidence.sprint_types` | 外部证据适用的 Sprint 类型；未列入的流程不读取该外部证据 | Step 0 PRE-FLIGHT |
| `manual_steps` | 需用户手工完成的步骤 | Step 2 EXEC |

Runtime 将任务唯一解析为 `execution_protocol=action|agent|orchestrator` 和 `review_protocol=agent-full|artifact-only`，并由 `harness task-context` 输出；规则可显式覆盖，但不得由 Agent 临时选择第二条执行链。`entry_action` / `execute.action` 统一通过 `task-action` 执行并写入当前 attempt 证据。独立 Review 后必须执行 `task-review`；`artifact_action` 只由 Review Gate 执行。旧 attempt、输入摘要变化、Action 失败、Review 非 PASS、报告或产物漂移均为硬阻断。

`product`、`design`、`backend-design`、`frontend-design` 和 `library-design` attempt 会绑定 `ARCHITECTURE.md` 的 SHA-256，并由 `task-context.architecture` 暴露；架构内容变化后，旧 attempt 立即失效，必须重新通过任务 Preflight，Plan/Exec/Review 不得继续使用旧架构输入。

验收项可显式写为 `{id, text}`；兼容字符串由 Runtime 生成确定性 ID，并通过 `task-context.acceptance` 暴露，Agent 不得自行编造或按数组序号引用。

---

## Sprint 计划文档

文件路径：`docs/exec-plans/active/sprint-N-name.md`；完成后移动到同名 `completed/`。任务级 Plan/Review 不属于知识库，只能进入 `.harness/runs/`。

**生命周期必填字段**：`sprint_type`、远端 `base_ref`、不可变 `base_sha`、`branch`；另含目标、状态、验收标准、依赖、关联 User Story

部署类 Sprint 额外声明 `source_sprints: [sprint-N-name, ...]`，用于绑定已批准的来源 Sprint、signoff 与提交；不得以接受报告目录非空代替。`walkthrough_env=test` 的 Feature Sprint 在自身验收前执行 Test 提升，不是已验收 Sprint 的部署编排，因此不读取 `source_sprints`，仍由当前计划依赖、分支与不可变提交约束交付来源。

**任务表基础列**：`| ID | 类型 | 来源 | 父任务 | 任务描述 | 依赖 | 产出物 | 验收条件 | 状态 |`；`依赖` 填逗号分隔的任务 ID，无依赖写 `—`。激活后新增任务只能经 `harness sprint amend --reason`，且来源为 `scope-split|remediation`、父任务必须是 amend 前已激活任务。既有 ID 的 type/origin/parent 不可改变；尚无 attempt 的可选阶段任务可因影响面收缩而移除并进入 retired 审计。旧状态若缺少身份快照或 planning contract，必须先恢复激活时计划并执行 `harness sprint migrate-state <plan> --reason <迁移依据>`；新 Sprint 只允许激活 v3。

**影响面选择**：Feature Sprint 在 `impact_surfaces` 写明变化及理由；Runtime 依据 `task-rules.yml#impact_surface_requirements` 反推设计任务与 facets，并要求与任务表精确一致。fullstack 是项目能力，不代表三类设计全部必选；不能用省略任务代替“无影响”声明。

`existing-boundaries-only` 是唯一不派生设计任务的影响面，仅适用于行为已由现有 PRD/设计/Architecture 覆盖、没有新边界或质量风险的实现增量；理由必须指明复用依据。product Review 若发现影响面不实，必须先 amend，不能继续 code。

**规则**：含 `code` 须包含质量、产品走查和闭环任务；任务依赖列引用真实上游任务 ID，产出物列声明路径。前端 code 只有消费本次变化的后端契约时才依赖对应后端任务，不按 project.type 机械串行。

---

## 任务派生与前置条件

`.harness/rules/task-rules.yml.sprint_type_sequences` 是不同 Sprint 流程的执行顺序真源，阶段以 `require: all|any` 明确全部完成或条件择一；`optional: true` 的设计阶段允许按 PRD 影响面整体省略或只列适用任务，但已列任务必须全部完成。`spawn_rules` 只负责派生建议。`harness sprint-gate` 在 Step 0 根据流程阶段、`prerequisites`、`prerequisites_any`、`external_evidence`、`upstream_outcome`、`readiness`、`approval_artifact` 和 `.harness/state/*` 执行硬校验。当前任务和必需前序阶段必须真实列入 Sprint，跨 Sprint 条件必须绑定具体输入证据。

编排者不得在 SPRINT.md 手写第二套派生链；需要变更流程时，优先修改 `task-rules.yml`，再只在本节补充流程解释。

---

## ⛔ L3 门控

`product-acceptance` 是 L3 门控任务，**必须先完整执行（Step 1→2→3），再呈现门控。**

### 执行流程

1. **执行任务**：`product-acceptance` 按标准 Plan → Exec → Review 流程执行
   - Exec 产出 **走查指南**（`sprint-N-walkthrough.md`）和 **走查报告模板**（`sprint-N-acceptance.md`）
   - 走查指南记录环境、路径、预期结果和截图占位
   - 走查报告记录 Boss 的实际结果、判定和结论
   - Review 验证走查包完整、结构正确、质量报告已覆盖当前迭代 E2E 与历史 P0 回归
2. **Review PASS 后**：输出走查摘要 + 走查指南路径，供 Boss 按指南操作
3. **使用 `ask_user` 工具阻塞等待**用户明确确认（`通过`/`approved`/`继续`）
4. **固化审批记录**：收到确认后执行 `uv run --project .harness/runtime harness acceptance-record approve <sprint-id>`；未通过则执行 `reject`
5. Boss 确认后先执行 `sprint-close` 归档，再由 `pr` 合并归档和交付物并安全清理 worktree

| 门控 | 触发时机 | 必要产出物 | 阻塞方式 |
|------|---------|-----------|---------|
| 产品走查 | `product-acceptance` Review PASS 后 | `sprint-N-walkthrough.md` + `sprint-N-acceptance.md` + `sprint-N-boss-signoff.yml` | `ask_user` 工具 |

### L3 执行约束

- 走查指南先于 Boss 走查生成，走查报告用于记录 Boss 结果
- `ask_user` 是 L3 的唯一放行动作
- `sprint-close` 以前，`sprint_gate.py` 必须校验签收文件和走查产物结构

**"没有反馈"等同于"未通过"。**

---

## 任务执行约束

1. **适用方案先行**：代码须依赖与真实影响面匹配的产品/技术方案；禁止无方案编码，也禁止为未变化的技术面生成占位方案
2. **质量门禁**：代码须通过质量评分后才能发布
3. **迭代增强优先**：Sprint N+1 在已交付模块上增强，禁止创建平行功能
4. **浏览器 E2E 不可替代**：前端须 `webapp-testing` 或 `chrome-devtools` 验证
5. **产出物模板约束**：规范定义的维度和权重不可修改

### 渐进式加载上下文

所有 Agent 必须先通过 `AGENTS.md` 导航与目录 `index.md` 检索候选文档，再按任务类型逐步加载正文，禁止一次性读取整个 `docs/` 知识库。

| 任务类型 | 历史文档使用规则 |
|---|---|
| PRD / 设计 / 技术方案 | 先读本次 User Story 与对应索引；只加载与当前需求强相关的历史文档。产出物是新的迭代文档，必须显式写清对旧设计的变更、优化、删除；不直接改旧 PRD/设计/技术方案。 |
| 多份相关历史文档 | 优先选择最近更新、验证状态为 `verified` 的文档；若逻辑冲突，以更新且更贴近当前需求的文档为参考，并在新文档记录取舍。 |
| Coding | 存在适用技术方案时，只读取 `upstream_inputs` 投影出的本轮技术方案与项目编码规范；PRD/设计只作为技术方案上游，需求摘要仅做漂移校验。流程明确没有技术方案时，使用显式最近依赖与项目基线，不自行检索历史或补造方案。 |
| 测试用例 | 与 Code 一样描述系统最新状态。生成前检索 `docs/test-cases/index.md` 与相关 YAML；业务流程变更时直接修改既有用例并更新 `last_modified_in` / `last_verified_in`，不得基于旧流程平行新增重复用例。 |

### 子任务拆分

**触发**（任一）：≥ 3 独立模块 | 验收条件 ≥ 5 | 产出 ≥ 500 行代码 | 同时涉及前后端

**规则**：单一职责、独立验收、依赖显式。编号：父 ID + 字母（T-14a, T-14b）。

---

## CI/CD 与发布编排

> 发布任务已经并入 Sprint 编排。`docs/CICD.md` 只解释 CI/CD 红线和策略，不再定义独立流程。

### Sprint 类型（H-OPT v1.7）

| 类型 | 触发 | 任务集 | worktree | 备注 |
|------|------|--------|---------|------|
| feature-sprint | 默认 | infra→product→适用设计→code→test-case-gen→quality→product-acceptance→sprint-close→pr | 是 | 设计任务按影响面选择；`walkthrough_env=test` 时在 quality 前插入 build-image/promote-test/integration |
| deploy-sprint(test) | 项目要求测试发布 | build-image→promote-test（自动 Preflight）→integration | 否 | 不开新分支，操作 `release-staging/<train>` |
| deploy-sprint(prod) | 项目要求生产发布 | …→release-prep→migration-design+regression→release-approval(L3)→prod-deploy→back-merge→observe | 否 | 含强制 L3 |
| hotfix | 线上故障 | hotfix-init→code→quality→prod-deploy→back-merge | 是 | 跳过 deploy-sprint 编排 |
| library-sprint | 公共包能力变更 | library-design→library-code→library-quality→library-package→library-contract→library-delivery→library-close→library-pr | 是 | 仅 `project.type=library`；package Action 后才允许消费者契约 |

### 编排者职责（取代独立 release Agent）

1. **判定 Sprint 类型**：根据用户指令 + `config/harness.yml.walkthrough_env` 决定流程
2. **deploy-sprint 启动**：feature-sprint `pr` 完成合并与安全清理后，按需启动 deploy-sprint（与 feature 互斥串行）
3. **配置门禁**：`build-image` 只依赖固定且已批准的来源 commit；`promote-test` Preflight 即时执行环境准备检查，未就绪则禁止部署

### 关键状态文件

```
.harness/state/
├── promote-prep-<env>.json   # promote-test 即时 Preflight/独立诊断结果
├── build-image-<sprint>.json # 镜像 tag + 构建结果
├── promote-test/<sprint>/<task-id>/<run-id>.json # 当前提升 attempt 的不可变构建/部署回执
├── promote-<env>-<sprint>.json # promote 执行日志
└── env-locks/<env>.lock      # 环境互斥锁
```

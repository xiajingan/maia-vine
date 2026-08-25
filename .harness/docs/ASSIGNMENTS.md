# Assignment 外部输入

跨工程协作有两条互斥入口：`Assignment` 是异步需求输入；`dependency session` 是消费工程当前 Sprint 内的同步公共包协作。治理模式仍是 `control/managed/standalone`；公共包工程使用 `project.type: library`，不增加第二套工程类型字段。

## 类型与所有权

| `assignment_type` | 典型来源 | 含义 |
|---|---|---|
| `product` | Control | 产品结果输入 |
| `architecture` | Control 或契约生产工程 | 架构、契约或迁移约束输入 |
| `dependency` | 消费工程 | 依赖工程需要提供的公共能力输入 |

源工程只通过分发端口写目标工程的 `docs/assignments/inbox/`；Harness 在目标 `.harness/state/assignments/` 保存幂等 receipt 与锁。目标工程在用户主动规划时决定是否纳入本地 `USER_STORIES.md` 和 Sprint，并独占写入 `docs/assignments/responses/`、`docs/deliveries/`。Assignment 输入本体不可原地改写，读取时重新计算摘要；同一幂等键不得指向不同 ID 或内容。

状态由三个真源派生，不维护第二套状态机：无响应为 `pending`；接受/调整响应为 `planned`；延期为 `deferred`；拒绝为 `rejected`；存在绑定同一 Assignment 的有效 Delivery 为 `delivered`。

## 最小输入

```json
{
  "schema_version": 2,
  "assignment_id": "seed-assignment-001",
  "assignment_type": "dependency",
  "source_project_id": "maia-mud",
  "source_reference": "mud-012",
  "target_project_id": "maia-seed",
  "outcome": "提供可跨 HTTP、消息和异步任务传播的可信租户上下文",
  "acceptance": [
    "公共 API 不包含 Mud 业务语义",
    "Seed 单元测试与 Mud 消费者契约测试通过"
  ],
  "priority": "high",
  "idempotency_key": "mud-012-seed-context-v1"
}
```

`source_reference` 指向源工程 Story 或 Sprint Task。依赖请求不填写未知的目标版本，也不规定目标工程内部设计；版本是交付结果。

## 使用流程

### 异步：Assignment

源工程生成并校验 Assignment 后显式投递：

```bash
uv run --project .harness/runtime harness assignment dispatch \
  docs/assignments/outbox/seed-assignment-001.json \
  --target-project ../maia-seed
```

目标工程仅在用户主动规划时处理：

```bash
uv run --project .harness/runtime harness assignment pending
uv run --project .harness/runtime harness assignment respond seed-assignment-001 accepted \
  --reason "属于无业务语义的公共上下文能力" \
  --local-story SEED-023 \
  --local-sprint sprint-4-seed-context
```

完成本地 Sprint、质量门禁和 Build Once 后，目标工程发布绑定 Assignment digest 的 Delivery。Delivery 使用完整 Git object ID 记录源码身份；同一 `delivery_id` 只允许同摘要幂等发布，禁止覆盖换包。公共依赖使用 `dependency-package` Artifact，必须包含稳定包名、精确版本、SHA-256、签名、SBOM 和构建证据；Artifact `ref` 以 `#sha256=<digest>` 绑定内容，不假设语言生态的文件名或扩展名。目标工程必须配置真实供应链 verifier，并产生绑定 Delivery digest 的 receipt。

Harness 只定义 verifier 的机器协议，不内置某个组织的信任根、制品仓库凭据或伪造“通过”的通用实现。Managed 工程在 `management.supply_chain_verification_commands` 配置，Standalone Library 在 `delivery.supply_chain_verification_commands` 配置。新安装的 verifier 列表为空是安全的 fail-closed 状态；工程必须在首次发布前提供调用真实签名/SBOM/provenance 服务的包装器并配置其信任策略，未配置时 `delivery verify` 必须拒绝执行。

源工程后续主动检查：

```bash
uv run --project .harness/runtime harness assignment status seed-assignment-001 \
  --target-project ../maia-seed
```

只有先存在有效 accepted Response、有效 Delivery 和独立供应链验证 receipt，状态才是 `delivered`，消费工程才能按精确版本和 SHA-256 更新锁文件。状态端口会验证 Response/Delivery 自身摘要、目标工程、Assignment digest、Artifact 身份及 receipt；任何同 ID 的无效文档均使状态 fail closed 为 `invalid`。Control 验证与组合 Release 时必须复用该完整状态门禁，并额外断言 Delivery 所在登记目录的所有者等于 Manifest `project_id`，不能只相信自报身份。不得使用 `latest`、Git 分支、本地 path 或复制依赖工程实现。

### 同步：coordinated dependency session

当消费需求必须在当前 Sprint 内完成且需要公共包变更时，在消费工程 Sprint 中增加一个 `dependency-change` 任务。Provider 必须在 `config/harness.yml.dependencies.providers` 登记 `orchestration: coordinated`，Provider 工程自身必须是 `project.type: library`，并为 capability 绑定消费方契约命令。Harness 按以下状态机执行：

`starting → provider-planning → candidate-built → consumer-verified → completed`

```bash
# 在消费工程的 Sprint worktree 中启动；Provider 会从最新 development 基线创建独立 Library Sprint worktree
uv run --project .harness/runtime harness dependency start mud-seed-retry-001 \
  --capability retry-policy \
  --consumer-sprint docs/exec-plans/active/sprint-2-wecom.md \
  --consumer-task S2-DEP-01 \
  --provider-project maia-seed \
  --provider-sprint sprint-3-retry-policy

# Provider 的 library-package Action 生成 Build Once 证据后登记候选；随后运行消费方契约
uv run --project .harness/runtime harness dependency candidate mud-seed-retry-001 \
  --artifact ../../maia-seed/.harness/worktrees/sprint-3-retry-policy/dist/maia_seed-1.2.3-py3-none-any.whl \
  --version 1.2.3
uv run --project .harness/runtime harness dependency verify-consumer mud-seed-retry-001

# Delivery 完成供应链验证，且消费工程锁文件精确锁定相同版本和 SHA-256 后闭环
uv run --project .harness/runtime harness dependency complete mud-seed-retry-001 \
  --delivery ../../maia-seed/docs/deliveries/seed-delivery-123.json \
  --lock uv.lock
```

Provider 与 Consumer 各自拥有 worktree、分支、任务重试和提交历史；session 只协调不可变请求摘要、candidate、消费者契约、Delivery 与锁文件，不允许跨仓库复制源码或共用 attempt。一个 Delivery 可通过 `satisfies.assignments[]` 与 `satisfies.dependency_sessions[]` 同时满足多个已验证输入，但每个绑定 ID 只能出现一次。

Python/uv 消费工程可使用内置 `uv.lock` 校验；其他包生态必须配置 `consumer_lock_command`。命令只能读取锁文件，并须在最后一行输出绑定 `lock/package/version/artifact_sha256/lock_sha256` 的 Lock Receipt JSON；Runtime 会复算锁文件摘要、拒绝命令修改锁文件，并持久化 receipt digest。

`request_digest` 同时绑定 consumer task 的任务描述和验收条件；Harness 将这些输入及摘要写入 Provider Sprint 计划。`library-package` 通过 `commands.package_build` 构建唯一 Artifact 并记录包名、版本、源码提交和 SHA-256；非 Python 构建命令须在最后一行输出 `artifact/package/version` JSON。没有该证据时 candidate 登记失败。`library-contract` Review Gate 只接受 Provider worktree 中同 session 的 `consumer-verified` 状态，因此执行顺序不能颠倒。

同一 `session_id + request_digest` 重复 `start` 是幂等读取；Provider 启动失败会保留 `failed` 证据，清理或修复 Provider worktree 后可用原命令重试。candidate 一经登记不可换包；契约或完成门禁失败时修复消费者/Delivery/lock 后重跑当前阶段，不新建第二个 session，也不覆盖历史摘要。

## 克制边界

- Assignment 不触发目标 Sprint，不提供后台监听或 Webhook；只有显式 `orchestration: coordinated` 的 dependency session 可同步创建 Provider Library Sprint。
- Control 不参与消费工程与公共依赖工程的日常开发交接；它只在系统组合时校验最终 Delivery、锁文件和供应链证据。
- Harness 不建立通用跨仓库任务 DAG；同步 session 仅阻塞声明它的 `dependency-change` 任务，异步 Assignment 不阻塞源工程。
- 临时业务实现及其删除责任留在源工程技术债；公共能力请求留在目标工程 Assignment 输入池，两者不可混为同一真源。

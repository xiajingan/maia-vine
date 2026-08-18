# Assignment 外部输入

Assignment 是工程之间统一、异步的需求输入。它只登记“目标工程下一次规划时需要评估什么”，不创建 Sprint、不启动 Agent，也不把两个工程合并成一个执行流程。

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

完成本地 Sprint、质量门禁和 Build Once 后，目标工程发布绑定 Assignment digest 的 Delivery。Delivery 使用完整 Git object ID 记录源码身份；同一 `delivery_id` 只允许同摘要幂等发布，禁止覆盖换包。公共依赖使用 `dependency-package` Artifact，必须包含稳定包名、精确版本、SHA-256、签名、SBOM 和构建证据；Artifact `ref` 必须包含与包名/版本一致的文件名并以 `#sha256=<digest>` 绑定内容。目标工程必须配置 `management.supply_chain_verification_commands`，通过 `delivery verify <manifest>` 执行真实签名、SBOM 与 Build Once 验证并在受管状态目录产生绑定 Delivery digest 的 receipt。

Harness 只定义 verifier 的机器协议，不内置某个组织的信任根、制品仓库凭据或伪造“通过”的通用实现。新安装的 verifier 列表为空是安全的 fail-closed 状态；工程必须在首次发布前提供调用真实签名/SBOM/provenance 服务的包装器并配置其信任策略，未配置时 `delivery verify` 必须拒绝执行。

源工程后续主动检查：

```bash
uv run --project .harness/runtime harness assignment status seed-assignment-001 \
  --target-project ../maia-seed
```

只有先存在有效 accepted Response、有效 Delivery 和独立供应链验证 receipt，状态才是 `delivered`，消费工程才能按精确版本和 SHA-256 更新锁文件。状态端口会验证 Response/Delivery 自身摘要、目标工程、Assignment digest、Artifact 身份及 receipt；任何同 ID 的无效文档均使状态 fail closed 为 `invalid`。Control 验证与组合 Release 时必须复用该完整状态门禁，并额外断言 Delivery 所在登记目录的所有者等于 Manifest `project_id`，不能只相信自报身份。不得使用 `latest`、Git 分支、本地 path 或复制依赖工程实现。

## 克制边界

- Assignment 不触发目标 Sprint，不提供后台监听、Webhook 或跨工程 Worktree 编排。
- Control 不参与消费工程与公共依赖工程的日常开发交接；它只在系统组合时校验最终 Delivery、锁文件和供应链证据。
- 源工程可以继续不依赖该能力的任务；是否等待由本地 Sprint 自己表达，Harness 不建立跨仓库任务 DAG。
- 临时业务实现及其删除责任留在源工程技术债；公共能力请求留在目标工程 Assignment 输入池，两者不可混为同一真源。

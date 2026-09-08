# Migration 规范

> 数据库与配置变更脚本的强制约束。专用于 `migration-design` 任务类型，由 deploy-sprint 的 `release-prep` 任务消费。
> 流程入口见 `docs/SPRINT.md`，任务规则见 `.harness/rules/task-rules.yml`。

---

## L2 集成中的迁移 producer

`migration-design` 和 `release-prep` 仍使用本页的可逆 Release SQL 资产与
`harness migration-check all`。`integration` 只有在选择 `data` 或 `schema` facet 且需要既有 Test 迁移 producer
时才声明 `config/harness.yml#integration.migration`；只读验证、环境修正或不涉及迁移的数据处理不需要该配置。
Test 环境若采用已批准的单向 checkpoint cutover，则声明：

```yaml
integration:
  migration:
    mode: checkpoint-one-way
    command: migration_dry_run
commands:
  migration_dry_run: [pnpm, test:migration-dry-run]
```

该 producer 必须在不修改代码和镜像的前提下，真实执行项目登记的 checkpoint、幂等续跑、validator 与清理
检查，并以非零退出拒绝失败。框架不接受 shell 字符串、空命令或未登记命令。此合同只定义 Test 环境迁移的
执行入口，不放宽 Release 的 pair/name/dry-down/idempotency 门禁；Test 迁移后的服务或质量问题修复后继续
验证，不自动回退数据。

---

## 目录与命名

```
templates/migration/                # 框架模板（项目从此处复制）
  ├── up.sql.tpl
  ├── down.sql.tpl
  └── README.md

deploy/release/<vX.Y.Z>/migrations/ # release-prep 产出物
  ├── 001-<slug>.up.sql
  ├── 001-<slug>.down.sql
  └── manifest.yml
```

**命名正则**：`^\d{3}-[a-z0-9-]+\.(up|down)\.sql$`

---

## manifest.yml Schema

```yaml
version: 1
release: vX.Y.Z
created_at: <ISO8601>
created_by: <agent-or-user>
execution:
  business_upgrade_rollback: forbidden
  progress_model: checkpoint
  candidate_on_failure: preserve
  resume_from: last-committed-checkpoint
  data_rollback: forbidden
  failure_phases: [migration, quality-check, service-start, release]
  rollback_authority: explicit-human-release-rollback
items:
  - id: 001-<slug>
    description: 一句话说明此次变更的业务动机
    forward: 001-<slug>.up.sql
    rollback: 001-<slug>.down.sql
    requires: []                    # 可选：依赖的 migration id 列表
    reversible: true                # 不可逆（如 DROP COLUMN）需声明 false 并附预案
    forward_compatible: true        # 旧版本应用读新 schema 是否仍可工作
    estimated_duration_seconds: 30
signature: <git-commit-sha>          # 由 migration_check.py sign 写入
```

---

## 强制约束（脚本校验）

| 约束 | 校验脚本子命令 | 触发位置 |
|------|---------------|---------|
| 执行策略与框架策略完全一致 | `harness migration-check policy <dir>` | quality + release |
| 每个 `up` 必有同名 `down` | `migration_check.py pair` | quality.yml + release.yml |
| 命名匹配正则 | `migration_check.py name` | 同上 |
| `down` 在 dry-run 时无 syntax error | `migration_check.py dry-down` | release.yml D7 |
| Forward + rollback 演练通过 | `migration_check.py rehearse --env test` | release.yml D7 |
| Manifest signature 匹配当前 commit | `migration_check.py sign` | release.yml D9 |

---

## 编写规则（正向定义）

1. **兼容预案**：`up.sql` 必有对应 `down.sql`，用于发布前演练和灾难恢复设计；运行中的迁移失败不得自动执行 `down.sql`。
2. **向前兼容**：旧版本应用读新 schema 应正常工作；破坏性变更采用双写窗口策略（先加列与双写 → 等到全量切换 → 再清旧列）。
3. **幂等**：脚本支持重复执行（`IF NOT EXISTS` / `IF EXISTS` 守卫）。
4. **幂等检查**：执行 `migration_check.py idempotency` 在 test 环境对脱敏快照执行 forward 两次，断言第二次无副作用。
5. **大表变更**：单批 ≤ 100k 行，分批使用稳定主键范围，并在每批完成后提交 checkpoint；不得用会随候选数据变化而漂移的 `OFFSET` 作为恢复位置。
6. **审计**：运行时进度写入 `.harness/state/migrations/<migration-id>.yml`，固定 candidate 引用并记录 checkpoint、失败阶段、恢复点和时间。

---

## 长任务与失败策略

业务升级与数据迁移是 checkpoint 驱动的长任务，而不是一次性事务。首次运行用 `harness migration-progress begin <migration-id> --candidate-ref <opaque-ref>` 固定由业务版本和候选数据共同构成的非敏感候选身份；每个可独立验证、可幂等重放的数据批次完成后，用 `checkpoint` 单调提交进度。进度还固定 `workflow_kind` 和有序 `required_checkpoints`，同一 ID 不能跨数据迁移、Standalone Pipeline 或 Control Release 复用。

迁移执行、质量检查、服务启动或发布命令应通过 `run <release-id> --phase <migration|quality-check|service-start|release> -- <argv>` 执行，非零退出会自动记录 `fail`；外部系统报告的错误则显式执行 `fail --phase ... --reason ...`。失败记录只改变运行状态，保持 `candidate_ref` 和全部 `committed_checkpoints` 不变；修复代码、配置或环境后执行 `resume`，从 `resume_from` 指向的最近已提交 checkpoint 继续。

运行中的失败禁止触发业务版本回滚、数据回滚、候选数据重建或重复备份。候选业务版本与已提交的数据迁移进度都保持原状；`down.sql` 只用于发布前演练或人员明确要求后的独立灾难恢复任务，不能作为迁移、质量门禁、启动探针或发布失败的自动补偿。只有人员在当前主交互中明确要求发布回滚，并生成绑定目标 Release 的 `source: ask_user` authorization receipt 后，框架才允许执行 `release-rollback`。

```bash
harness migration-progress begin release-vX.Y.Z --candidate-ref candidate-vX.Y.Z
harness migration-progress run release-vX.Y.Z --phase migration -- python scripts/migration_runner.py next-batch
harness migration-progress checkpoint release-vX.Y.Z batch-0001 --summary "主键 1..100000 已迁移并验证"
harness migration-progress fail release-vX.Y.Z --phase quality-check --reason "数据一致性阈值未通过"
# 修复检查逻辑或候选数据中的问题；不还原数据
harness migration-progress resume release-vX.Y.Z
harness migration-progress status release-vX.Y.Z
# 手工 Standalone Production 发布使用独立 release workflow；Pipeline/Control 会自动创建隔离 ID
harness migration-progress begin deploy.release-vX.Y.Z --candidate-ref candidate-vX.Y.Z --workflow-kind manual-release --required-checkpoint deploy-default --required-checkpoint watch-default
harness deploy --env prod --tag candidate-vX.Y.Z --progress-id deploy.release-vX.Y.Z
harness deploy watch --env prod --tag candidate-vX.Y.Z --progress-id deploy.release-vX.Y.Z
harness migration-progress complete deploy.release-vX.Y.Z
```

---

## AI 执行协议（`migration-design` 任务）

**允许工具**：bash（`migration_check.py`）、文件读写、SQL 客户端 dry-run。
**禁止**：在 dev / prod 环境直接执行 forward。

**步骤**：
1. 加载技术方案（数据模型变更）+ 上一 release manifest（依赖关系）。
2. 生成 `up.sql` + `down.sql` + manifest 条目。
3. 分别执行 `harness migration-check policy|pair|name|dry-down|idempotency <dir>` 中列出的五个 action。
4. 提交至 `deploy/release/<vX.Y.Z>/migrations/`。

**验收**：以上 4 项校验子命令退出码全为 0。

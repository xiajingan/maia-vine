# 发布规范（v1.6 兼容说明）

> v1.6 起不再存在独立 `release` 任务，也不再使用 `harness-release` Agent。
> 发布被拆成 deploy-sprint 中的结构化任务，并由主 Agent 按 `docs/SPRINT.md`
> 和 `.harness/rules/task-rules.yml` 编排。

## 当前发布任务链

生产发布使用以下任务类型：

1. `release-prep`：生成 release notes、迁移清单、质量摘要
2. `migration-design`：设计并校验数据库迁移
3. `regression`：执行 L3 回归、性能与安全检查
4. `release-approval`：L3 上线许可，必须 `ask_user` 明确确认
5. `prod-deploy`：生产部署与健康窗口；失败保留候选、修复后续跑，不自动回滚
6. `back-merge`：main → test/develop 回灌
7. `observe`：线上观测报告

发布资产统一写入 `deploy/release/<vX.Y.Z>/`；旧顶层 `release/` 目录由安装脚本迁移到 `.harness/migrations/`，避免继续作为新产出目录。

涉及业务升级和数据迁移时，框架不得主动回滚。只有人员在当前主交互中明确要求发布回滚，并生成绑定目标 Release/tag 的 authorization receipt 后，才能执行回滚命令。

先执行 `harness release-authorization challenge <authorization-id> --target-ref <release-id|run-id|tag> --candidate-ref <manifest-digest|tag> --interaction-id <当前主交互-id>` 生成不可变 nonce。人员明确答复后，主编排运行时以工程外密钥 `HARNESS_INTERACTION_PROOF_KEY` 对包含 `decision/source/requested_by/requested_at/expires_at/reason/challenge_sha256/interaction_id` 的交互事件签名，将 HMAC-SHA256 写入 `interaction_proof`，再执行 `harness release-authorization issue <authorization-id> --signoff <path>`。签发会拒绝 challenge 之前、超过 15 分钟或已过期的 signoff；回滚入口只接受 `.harness/state/rollback-authorizations/issued/` 中的 receipt。

交互 broker 契约是：删除 `interaction_proof` 后按 UTF-8、键排序、无多余空白的 JSON 序列化，并计算 HMAC-SHA256；broker 是主编排层能力，不得在工程 Agent 或 Harness CLI 中提供签名入口，CLI 的 `issue` 只验证该证明。部署环境必须以进程级 secret 注入验证密钥，禁止写入工程文件。

签发后的授权包含 interaction、nonce、Release/candidate 绑定、可信交互证明摘要、signoff 摘要和 receipt 摘要，并由运行时密钥对完整授权字段额外计算 `authorization_hmac`；每次消费都会重新验证完整 HMAC、原始 challenge 摘要及全部身份字段。同一授权在首次执行时原子绑定到对应 Control Release、Pipeline run 或单节点部署，不能改绑目标、候选或其他回滚操作。Harness CLI 只验证主编排运行时提供的外部 `ask_user` 证明，不生成签名、不持久化证明密钥，也不从工程内 YAML 推断人员决定。

## 单一真源

| 内容 | 真源 |
|---|---|
| 发布任务流 | `docs/SPRINT.md` |
| 任务类型、门控、产出物、验收条件 | `.harness/rules/task-rules.yml` |
| CI/CD 红线、分支/环境策略、故障速查 | `docs/CICD.md` |
| 部署环境与 secret 声明 | `config/deploy.yml` |

如果历史 Sprint 计划仍包含 `release` 任务，应迁移为上述任务链。

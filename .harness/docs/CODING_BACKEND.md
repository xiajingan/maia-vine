# 后端编码规范

本轮实现以有效 Ready/Done 技术方案与当前 ARCHITECTURE.md 为设计依据。发现职责、模型、接口或关键机制需要变化时，按 [设计治理协议](DESIGN_GOVERNANCE.md) 修订本 Sprint 的原方案，经重审和人工确认后更新架构；仅实现缺陷直接修复代码。完成 Review 对照架构约束、实际代码落点与验证证据进行双向核对。

> 后端 `code` 任务的编码约束与完成标准。专用于 `code` 任务类型（后端）。
> 技术方案见 [TECH_BACKEND.md](TECH_BACKEND.md)，线上观测见 [OBSERVABILITY.md](OBSERVABILITY.md)。

---

## 上下文边界

存在适用后端技术方案时，Coding 的直接派生输入只有 `task-context.upstream_inputs` 投影出的本轮后端技术方案；PRD/设计是技术方案的上游，不再与技术方案并列指导实现。USER_STORIES 摘要仍用于漂移门禁，但 `requirements.access=integrity-only` 时不得直接回读需求补充实现。不得回读历史 PRD/设计/技术方案；历史变更、优化、删除必须已经沉淀在本轮技术方案中。流程明确不产生技术方案时，只使用 task-context 的显式直接输入和项目编码基线，不自行检索或补造设计。

---

## 可靠性

### 错误处理

- **禁止空 catch**，至少记录日志
- 使用 `ARCHITECTURE.md` 声明的错误 envelope 和项目异常边界
- 公共 Library 只提供技术 envelope/基础异常；业务错误码定义在拥有语义的领域契约或业务模块
- 禁止暴露 stack trace

### 日志

- 复用 Architecture Provider；关联 ID、级别、字段和脱敏遵循项目可观测性/数据分类基线

### 重试 / 幂等 / 并发

- 只实现技术方案中由副作用、依赖契约和负载证据触发的超时、重试、幂等、并发控制或补偿
- 重试次数/退避服从端到端时延预算；不可安全重试的操作必须拒绝重试或先建立幂等语义
- 幂等存储、TTL、锁、队列和缓存技术服从 Architecture，不默认绑定 Redis
- 无明确容量或热点证据时不为“高并发”提前异步化

---

## 安全

| 领域 | 要求 |
|------|------|
| 认证/授权 | 复用 Architecture 声明的认证 Provider；业务模块实现主体/资源/动作授权，不假定 JWT 或固定角色 |
| 输入验证 | 在项目声明的信任边界使用 Schema/类型模型；URL、文件等高风险输入按实际能力校验 |
| 数据安全 | 参数化查询、数据分类、最小权限、敏感字段保护遵循项目基线 |
| 接口安全 | Rate Limit、CORS、安全 Header 等仅按项目暴露面和安全基线启用 |
| 密钥 | 禁止提交代码仓库、仅提交 `.env.example`、使用项目声明的 Secret Manager、依赖漏洞扫描无 High/Critical |

涉及认证/支付/访问控制变更须安全专项审查。

---

## 数据访问与业务 ID

涉及数据库或业务 ID 时必须读取 [Golden Rules G-9～G-11](GOLDEN_RULES.md#g-9g-11-适用边界与验证)，执行 Architecture 与本轮技术方案声明的具体契约。

- 业务授权、状态迁移、校验、计算口径和编排由领域/应用代码拥有；按项目约束使用数据库事务、查询和声明式完整性约束，不新增隐藏业务执行。
- 使用项目指定的 ORM/query builder，将驱动与方言细节隔离在数据访问边界；Schema、Repository、SQL 和迁移归所属业务工程。必要原生 SQL 集中、参数化，不能通过 ORM/raw SQL/数据库脚本绕过禁用能力。
- 对实际生成和执行的 SQL 验证方言、版本与目标数据库行为；类型检查通过不等于 SQL 兼容，迁移的执行和回退风险同样需检查。
- ID 沿用项目选定算法及存储/传输表示，数据库驱动、DTO、事件、队列和消费者保持无损；外部与观测协议 ID 单独建模。采用分布式整数发号时，按方案实现节点资格、时钟、重启与序列边界处理，不以单进程无重复测试证明全局唯一。

---

## 完成标准（DoD）

- `config/harness.yml` 中登记的 lint、format、typecheck、unit/integration 命令全部通过，覆盖率 ≥ 80%
- `uv run --project .harness/runtime harness verify health` 通过（API 返回 200）
- fullstack 项目中涉及用户可见链路的变更须有 `tests/e2e/scenarios/` 下对应的 Playwright 场景；backend 使用 API 集成测试
- 新增/变更 API 已更新文档、`.env.example` 已更新、DB 迁移文件已新增
- 数据访问、迁移或 ID 契约发生变化时，受影响的 SQL/目标数据库兼容、ID 无损往返及适用的生成器故障验证有真实结果；无关变更不重复运行专项验证
- 技术债记录到 `tech-debt-tracker.md`

---

## AI 执行协议

**允许工具**：文件读写/搜索、bash（build/test/lint）、子代理、项目语言对应的后端最佳实践 | **禁止**：修改规范文档

**代码生成约束清单**：
- 实现与 Story/AC、技术方案和 Architecture Profile 一致，不新增未设计的层、抽象或中间件
- 错误处理：无空 except/catch，使用项目错误边界，敏感信息不泄漏
- 重试/幂等/并发：仅实现技术方案已触发策略，并覆盖其副作用与失败测试
- 安全：认证授权、边界校验、参数化查询和密钥管理服从项目暴露面与安全基线
- 数据与 ID：遵守 G-9～G-11 及项目具体约束，不默认指定数据库、ORM 或 ID 算法，不将尚未实现的检查写成已通过的 Lint

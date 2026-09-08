# Library 技术方案规范

> 专用于 `project.type: library` 的 `library-design` 任务。公共能力归属以项目 `ARCHITECTURE.md` 为真源，执行流程以 `SPRINT.md` 和 `ASSIGNMENTS.md` 为真源。

**产出物**：`docs/tech-docs/` 下单份不超过 300 行的技术方案，并在项目自有索引以当前 Task/Run 登记 library `draft`、模块、API/组件和 Story/AC；Review PASS 后由 Runtime 发布。新 Sprint 不改写旧产物，覆盖 Scope Key 时用 `Supersedes` 指向 current Entry。只写公共契约和本次增量。

每份技术方案必须包含唯一的 `## 作用域清单`，并与 `docs/tech-docs/index.md` 当前 Task/Run 登记逐行一致；不适用的表/API 或组件写 `—`，两者不能同时为空：

| Scope Key | 模块 | 表/API | 组件 |
|-----------|------|--------|------|
| tech:example | example | example-api | — |

## 设计边界

- Library 只提供跨消费者稳定能力，不包含任一消费者的业务实体、流程、业务状态或配置默认值。
- 公共技术 Library 可提供错误/Event envelope、上下文传播、配置加载、安全/加密适配、审计/OTel 接入和中间件连接能力；具体业务错误码、领域事件、状态值、授权策略、Repository 与缓存一致性语义留在拥有它们的领域契约或业务工程。
- 提取前必须存在 `ARCHITECTURE.md` 指定的平台 Provider，或多个已确认消费者；“以后可能复用”不足以创建 Library。
- 公共 API 和协议必须有稳定 capability ID；内部实现不作为契约。错误、事件和重试语义只有确属跨消费者协议时才进入公共契约。
- 消费工程只保留适配层，不复制 Provider 源码，不通过 Git 分支、本地 path 或 `latest` 依赖。
- 破坏性变更必须升级 major 版本；兼容扩展使用 minor，修复使用 patch。

## 最小充分设计

- 先说明现有 Provider/消费者为何不能承载，再新增 package、模块或公共抽象。
- 只抽象稳定共同语义；消费者差异由各自适配层承担，不用巨型配置统一业务流程。
- 可替换指公共契约不泄漏底层厂商细节，不要求为每个依赖再包装一层无价值接口。
- 每个新增公开符号必须关联 capability 和真实消费者，并具有契约验证；无消费者的推测性 API 不发布。

## 必须章节

1. **来源、能力与所有权**：Story/Assignment、capability ID、问题、Provider、现状缺口和明确排除的业务语义。
2. **最小公共契约**：公开类型/协议、输入输出、不变量和错误模型；新增符号关联真实消费者，禁止暴露内部模块。
3. **兼容与回退**：当前/目标版本、废弃窗口、迁移和上一不可变版本回退方式。
4. **实现与验证**：模块职责、可替换边界、第三方依赖、消费者契约和交付方案。

消费者矩阵、废弃窗口、迁移、SBOM/签名等只在受影响或交付配置要求时展开；未触发的章节不以空表占位。

## Review 门禁

- 四个核心章节完整且与 `ARCHITECTURE.md`、`config/harness.yml.dependencies.providers` 一致；条件内容只在触发后检查。
- 每个受影响消费者都有登记的 `consumer_contract_commands`，命令缺失时不得进入实现。
- API 不含消费者业务语义；技术 envelope 与具体业务错误、事件、状态的所有权已分离。
- 方案明确向后兼容或 major 升级，不以“调用方同步修改”替代版本策略。
- 回退使用上一不可变 package 版本，不依赖删除远端制品或改写相同版本。

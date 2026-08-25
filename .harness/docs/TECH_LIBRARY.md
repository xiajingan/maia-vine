# Library 技术方案规范

> 专用于 `project.type: library` 的 `library-design` 任务。公共能力归属以项目 `ARCHITECTURE.md` 为真源，执行流程以 `SPRINT.md` 和 `ASSIGNMENTS.md` 为真源。

**产出物**：`docs/tech-docs/` 下单份不超过 300 行的技术方案，并更新 `index.md`。不涉及的服务端数据库、缓存、部署和 UI 章节不得复制进来。

## 设计边界

- Library 只提供跨消费者稳定能力，不包含任一消费者的业务实体、流程或配置默认值。
- 公共 API、事件、错误码和重试语义必须有稳定 capability ID；内部实现不作为契约。
- 消费工程只保留适配层，不复制 Provider 源码，不通过 Git 分支、本地 path 或 `latest` 依赖。
- 破坏性变更必须升级 major 版本；兼容扩展使用 minor，修复使用 patch。

## 必须章节

1. **能力与所有权**：capability ID、问题、Provider、明确排除的业务语义。
2. **公共契约**：公开类型、函数/协议、输入输出、不变量和错误模型；禁止暴露内部模块。
3. **兼容策略**：当前版本、目标版本、废弃窗口、迁移与回退方式。
4. **实现结构**：模块职责、依赖方向、可替换边界和第三方依赖。
5. **消费者矩阵**：受影响工程、使用入口、契约命令和兼容预期。
6. **交付方案**：package 名称、Build Once 命令、Delivery、签名、SBOM 和版本锁定。

## Review 门禁

- 六章节完整且与 `ARCHITECTURE.md`、`config/harness.yml.dependencies.providers` 一致。
- 每个受影响消费者都有登记的 `consumer_contract_commands`，命令缺失时不得进入实现。
- API 不含消费者业务语义；公共错误、事件和重试行为只有一个实现真源。
- 方案明确向后兼容或 major 升级，不以“调用方同步修改”替代版本策略。
- 回退使用上一不可变 package 版本，不依赖删除远端制品或改写相同版本。

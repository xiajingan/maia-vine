# Code Review

> 代码评审规范。专用于 `review` 任务类型。
> 编码规范见 [../PROJECT_RULES.md](../PROJECT_RULES.md)，后端约束见 [CODING_BACKEND.md](CODING_BACKEND.md)。

**产出物**：`docs/review-reports/sprint-N-review.md`（须更新 `index.md`）

---

## 评审维度体系（6 维）

| 维度 | 参考规范 | 核心检查 |
|------|---------|---------|
| 编码规范 | PROJECT_RULES.md Key Rules + Checklist | 全部条目 + 模块职责单一、复杂逻辑有注释 |
| 架构一致性 | 技术方案 + TECH_BACKEND/FRONTEND.md + 设计文档 | 分层正确、公共能力来自 ARCHITECTURE 登记的 Provider、无跨仓库源码复制或重复 helper、实现与方案一致、前端 DOM 结构与原型对齐 |
| 安全合规 | CODING_BACKEND.md 安全章节 | 认证、输入校验、数据安全、接口安全、密钥管理 |
| 可靠性 | CODING_BACKEND.md 可靠性章节 | 错误处理、日志、重试、幂等、并发 + 前端 onUnmounted 清理 |
| 性能 | PROJECT_RULES.md 代码质量基线 | 无重复 IO、异步队列、无大表 JOIN、缓存规范、前端无多余重渲染 |
| 可维护性 | PROJECT_RULES.md 编码约束 | 函数 ≤ 50 行、圈复杂度 ≤ 10、无魔法数字、async/await |

## 严重级别

| 级别 | 定义 | 处理 |
|------|------|------|
| Critical | 安全漏洞、数据泄露、生产事故风险 | 必须修复 |
| Major | 违反核心规范、架构不一致、可靠性缺失 | 必须修复 |
| Minor | 可维护性、非关键性能 | 建议修复，不阻塞 |
| Suggestion | 优化建议 | 记录 tech-debt-tracker.md |

**通过标准**：0 Critical + 0 Major + Minor ≤ 5 + `config/harness.yml` 当前 project.type 静态检查通过

严重级别必须由实际影响决定，而不是由逻辑是否“足够完美”决定：

- Critical：已复现或存在直接可利用路径的数据损坏、密钥泄露、权限绕过或生产事故
- Major：违反适用验收或核心不变式，并对业务正确性或质量属性产生实质影响
- Minor：局部可维护性、测试脆弱性或非关键性能问题
- Suggestion：进一步加固、尚未验证的风险或范围外优化

每个阻断项必须给出“不变式 → 场景 → 可观察故障 → 质量属性”的因果链。适用质量属性包括正确性、可靠性、可用性、可扩展性、并发安全、安全、可维护性、架构简单性、性能和可运维性；未被任务方案、SLO 或威胁模型声明适用的属性不得自动升级为门禁。

## 范围、基线与证据

- Full Review 覆盖全部适用验收 ID；深度代码检查限定为任务 diff、直接影响面、既有 finding 与明确不变式，不要求穷举所有辅助实现
- 新文件完全遵守当前规范；遗留文件未被任务扩大风险时采用 no-worse 基线，除非规范明确声明绝对门禁
- Review remediation 是非权威建议；Plan 必须按根因和原始验收裁决，不得把建议自动转成新标准
- 缺少证据不等于已证明产品缺陷。只有证据、环境或范围缺口时结论为 INCOMPLETE

## 测试代码原则

- 普通单元测试证明一个明确测试点，通过更多独立用例扩展覆盖，不要求具备业务服务级防御能力
- 集成/E2E 必须经过所声明的真实入口并观察关键状态和结果，但不递归要求所有 helper 达到生产组件标准
- 迁移、发布、安全门禁可能允许破坏性操作或制造假 PASS，按其实际风险严格审查
- 测试辅助代码只有在会掩盖关键失败时判 Major；局部脆弱性通常为 Minor/Suggestion

---

## 完成标准（DoD）

- 6 维全部检查
- 评审报告已生成（结论 + 问题清单 + 级别）
- Critical/Major 已修复并验证
- 修复后 `command_groups.precommit` 与受影响构建命令通过

---

## AI 执行协议

**允许工具**：搜索、文件读取、bash（typecheck/lint/test/audit）、code-review 子代理 | **禁止**：创建新业务文件

**执行步骤**：
1. 确定适用 facets、范围和基线（`git diff --name-only`）
2. 加载规范（PROJECT_RULES + ARCHITECTURE + CODING_BACKEND/FRONTEND + 技术方案）
3. 静态分析（执行 `config/harness.yml` 当前 project.type 的 lint/typecheck，并运行适用的依赖审计）
4. 6 维 × 每个变更文件逐项检查；只对适用维度出具阻断结论
5. 若涉及公共包，核对 dependency session/Assignment 摘要、不可变 Delivery、消费者契约证据及 lock 中的精确版本+SHA-256
6. 生成报告 → 判定通过/不通过 → 不通过派生修复任务

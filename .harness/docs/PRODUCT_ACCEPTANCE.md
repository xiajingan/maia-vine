# Product Acceptance（产品走查规范）

> 测试环境产品走查。专用于 `product-acceptance` 任务类型。
> 产品原则见 [PRODUCT_SENSE.md](PRODUCT_SENSE.md)，设计规范见 [DESIGN.md](DESIGN.md)。

**产出物**：`docs/acceptance-reports/sprint-N-walkthrough.md` + `sprint-N-acceptance.md`  
**审批记录**：Boss 确认后生成 `docs/acceptance-reports/sprint-N-boss-signoff.yml`

## 前置条件

| 条件 | 验证方式 |
|------|---------|
| 环境就绪 | `uv run --project .harness/runtime harness verify preflight` 全部 ✅（基础设施 + DB + 环境变量） |
| 质量评分 ≥ 95 | `quality` 任务报告 |
| 当前迭代 E2E 通过 | 质量报告含 `当前迭代: ✅ 通过` |
| 历史 P0 回归通过 | 质量报告含 `P0 回归: ✅ 通过` |
| 存在 live 用例时真实链路通过 | 质量报告含 `真实链路: ✅ 通过` |
| PRD + 适用设计文档存在 | `product-specs/` + Sprint 实际列入的 `design-docs/tech-docs/` |
| 项目运行环境就绪 | `uv run --project .harness/runtime harness verify health`；只检查当前 project.type 的适用服务 |

> Preflight 失败时，输出缺失项和配置指引，等待用户完成配置后重新运行。

## 测试环境启动协议

> 产品走查在项目实际运行环境下执行；backend/API、frontend 或 fullstack 使用各自可观察入口，不强制启动无关服务。

1. **Preflight**：`uv run --project .harness/runtime harness verify preflight` — 校验基础设施、数据库、环境变量
2. **启动服务**：`uv run --project .harness/runtime harness verify health` — 启动并检查当前 project.type 的适用服务
3. **Preflight 或 Health 任一失败** → 输出具体修复命令 → 等待用户确认后重试

## 偏差严重级别

| 级别 | 定义 | 处理 |
|------|------|------|
| 🔴 Critical | 功能缺失/不可用，阻断主路径 | 必须修复 |
| 🟠 Major | 与 PRD/设计明显偏差，影响体验 | 必须修复 |
| 🟡 Minor | 细节偏差，不影响主路径 | ≤ 5 个可放行 |
| 🔵 Observation | 优化建议 | 不阻碍发布 |

**通过标准**：0 Critical + 0 Major + Minor ≤ 5 + 纳入范围的 Story/AC 覆盖 100% + 产品决策质量 ≥ 4/5 + 适用状态覆盖

## 走查维度

F 与 P 对所有 Feature Sprint 适用；U 只在 PRD/设计声明 UI 变化时适用；X 只检查当前入口可观察且与本次变化有关的体验。不可达或未触发项记录范围依据，不生成占位用例，也不因 project.type 自动判为适用。

### 维度一：PRD 功能符合性（F-01~F-08）

| 编号 | 检查内容 | 判定标准 |
|------|---------|---------|
| F-01 | 主路径完整 | 按 PRD 用户流程逐步执行无阻断 |
| F-02 | 边界行为覆盖 | PRD 纳入范围的真实边界和恢复行为符合 AC |
| F-03 | 契约完整性 | 本次变化的字段/输入输出与 PRD 一致 |
| F-04 | 状态适用性 | 本次变化可达且用户需处理的状态均有正确行为 |
| F-05 | 默认值策略 | 与 PRD 一致，体现产品策略 |
| F-06 | 文案准确性 | 与 PRD 定义一致 |
| F-07 | 数据埋点 | PRD 定义的埋点全部触发 |
| F-08 | 质量属性 | PRD/Architecture 对本次声明的指标与风险验证通过 |

### 维度二：UI 设计符合性（U-01~U-08）

| 编号 | 检查内容 | 判定标准 |
|------|---------|---------|
| U-01 | 受影响页面流 | PRD 影响范围内页面已实现，导航路径与设计证据一致；无 UI 变化时记录范围依据 |
| U-02 | 视觉层级 | 信息层级与设计一致 |
| U-03 | 布局与间距 | 关键布局与项目 token/设计对照一致 |
| U-04 | 组件状态 | 本次变化直接影响的可达状态有设计与实现证据 |
| U-05 | 响应式 | 项目目标设备/Profile 的受影响断点正确 |
| U-06 | 动效 | 设计已定义的必要动效与实现一致；未定义时不要求 |
| U-07 | Design Token | 可追溯到 token，无硬编码样式 |
| U-08 | 业务优先级 | 视觉重点、动作和关键后果符合项目策略与 PRD |

### 维度三：产品决策符合性（P1~P5）

> 每项独立评分，均分 ≥ 4/5 通过。项目策略来自 `USER_STORIES.md`，通用判断方式来自 [PRODUCT_SENSE.md](PRODUCT_SENSE.md)。

| 主张 | 核心检查 |
|------|---------|
| P1 场景与结果保真 | 保持 Story 的使用者、触发、约束和可观察结果 |
| P2 最小充分范围 | 没有无 AC/风险来源的功能、步骤或系统复杂度 |
| P3 项目策略一致 | 符合 USER_STORIES 声明的适用产品策略及代价 |
| P4 状态与行动清晰 | 用户理解当前状态、可执行动作和关键后果 |
| P5 验收与证据 | Story/AC、实现行为和实际观察双向追溯 |

### 维度四：用户体验符合性（X-01~X-05）

| 框架 | 检查项 |
|------|--------|
| 认知负担 | X-01：只保留当前任务必需决策，不隐藏专业控制项 |
| 反馈 | X-02：按项目指标明确动作、进度或结果 |
| 恢复与可逆性 | X-03：摩擦、确认和恢复与真实风险匹配 |
| 一致性 | X-04：复用项目既有交互、组件和文案语义 |
| 文案 | X-05：具体说明状态、原因或动作，不使用无信息泛语 |

---

## 用例生成指引

> AI 从检查项 + PRD + 设计文档动态生成走查用例，不使用预置用例。

为当前 Sprint **适用的检查项**生成可执行用例；不适用项记录范围依据，不生成占位步骤。格式：`PA-维度-编号` | 验证方法 | 执行命令/人工操作 | 期望结果 | 判定

涉及既有页面改造时，走查用例需同时回答两件事：

1. 设计稿本身是否清晰、有序、可直接对照
2. 代码实现是否忠实还原设计稿

---

## 完成标准（DoD）

- 走查指南已保存至 `docs/acceptance-reports/sprint-N-walkthrough.md`
- 走查报告模板已保存至 `docs/acceptance-reports/sprint-N-acceptance.md`
- Boss 完成走查后，审批记录已保存至 `docs/acceptance-reports/sprint-N-boss-signoff.yml`
- 结论满足：0 Critical + 0 Major + 产品决策质量 ≥ 4/5

走查报告必须包含两个机器可读表：

- `## 检查项结果`：`检查项 | 结果 | 范围依据/证据`，唯一覆盖 `walkthrough-checks.yml` 的全部 ID；`applicability=required` 只能为 `pass`。`conditional` 的 `applies_when` 会匹配 Sprint `impact_surfaces` 与 `project.type`，匹配后同样只能为 `pass`；未匹配才可写 `n/a`，且必须给出具体范围依据（“本次不适用”不构成依据）。
- 产品决策评分：P-1～P-5 各且仅各一项，使用 `P-1 … 4/5` 格式。缺项、重复项或只写总分均不能通过。

---

## AI 执行协议

**允许工具**：bash、走查报告文件读写、适用浏览器/API 验证工具 | **禁止**：代码修改、PRD/设计/技术规范修改

**执行步骤**：
1. 前置检查（质量评分达项目阈值、当前迭代适用测试通过、P0 回归通过、真实链路通过〔若存在 live 用例〕、PRD/适用设计存在、环境可访问）
2. 生成走查用例
3. 生成**走查指南**（`docs/acceptance-reports/sprint-N-walkthrough.md`），供 Boss 按步操作
4. 生成**走查报告模板**（`docs/acceptance-reports/sprint-N-acceptance.md`），用于记录 Boss 走查结果
5. Boss 确认后，由编排者执行 `uv run --project .harness/runtime harness acceptance-record approve|reject <sprint-id>`；approve 会同时执行结构、规则项覆盖和评分门禁，再固化审批结果

### 走查指南规范

走查指南是面向 Boss 的操作手册，记录操作与预期，不记录执行结果：

| 章节 | 内容 |
|------|------|
| 环境信息 | 测试 URL、账号、版本号 |
| 变更对照 | PRD + 适用技术/UI 设计路径 + 当前/目标行为核心比对点 |
| 功能走查路径 | 按 PRD 主路径编排的逐步操作指南；UI 使用截图，API/后台任务使用响应、状态或日志证据 |
| 呈现质量检查 | UI 检查布局/层级；API/数据/流程检查契约可读性、状态与关键证据 |
| 每步包含 | 操作说明 → 预期结果 → 适用证据占位 |
| 注意事项 | 数据准备、浏览器/设备要求、重试方式 |

涉及头像/图片/上传等媒体能力时，走查指南须覆盖：上传成功 → 跨页回显 → 刷新后持久化。
涉及 `execution.mode: live` 的真实链路时，走查指南须引用最新 live smoke 证据，并明确 Boss 本轮需要复核的真实接口结果。

### 走查报告规范

走查报告用于记录 Boss 的实际结果与结论：

| 章节 | 内容 |
|------|------|
| Boss 走查记录 | 步骤 → 实际结果 → 判定 |
| 偏差清单 | Critical / Major / Minor / Observation |
| 结论 | pass / fail + 摘要 |
| 审批记录 | 关联 `sprint-N-boss-signoff.yml` |

### ⛔ BOSS 产品走查（L3 门控）

Boss 走查阶段的标准动作：

1. 输出走查摘要 + 测试环境 URL + 走查指南路径
2. 使用 `ask_user` 阻塞等待 Boss 明确确认
3. 收到 `通过` / `approved` 后，执行 `uv run --project .harness/runtime harness acceptance-record approve <sprint-id>`
4. 收到未通过结论后，执行 `uv run --project .harness/runtime harness acceptance-record reject <sprint-id>` 并回到修复流程

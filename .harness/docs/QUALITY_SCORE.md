# Quality Score

> 测试质量评分规范。专用于 `quality` 任务类型。
> 代码评审见 [CODE_REVIEW.md](CODE_REVIEW.md)。
>
> `quality` 任务入口、评分权重与 case 归因以 `harness quality-score` 为可执行真源。
> L1/L2/L3 的触发点与任务流由 `docs/SPRINT.md` + `.harness/rules/task-rules.yml` 决定。

**产出物**：`docs/test-reports/sprint-N-quality.md`（须更新 `index.md`）

## 执行入口

```bash
# 默认 L1（develop / sprint / promote 分支）
uv run --project .harness/runtime harness quality-score --sprint 5-mock-adapter

# L2（test / release 分支）：增加集成 + migration 校验
uv run --project .harness/runtime harness quality-score --sprint 5-mock-adapter --level L2

# L3（main 合入前）：全量回归 + 性能 + 观测
uv run --project .harness/runtime harness quality-score --sprint 5-mock-adapter --level L3
```

`--sprint` 参数必填，脚本据此生成 Sprint 专属报告文件 `sprint-N-quality.md`。

脚本计算 7 个评分维度并生成报告。**Python 实现是评分逻辑的唯一真相源**（权重来自配置，执行和计算方式以代码为准）。

## 报告内容

脚本自动生成的报告包含：

1. **Sprint 上下文**：Sprint 标识、生成时间、阈值、结果
2. **评分明细表**：7 个维度的得分 / 满分 / 权重
3. **维度详情**：实际执行命令数量、测试/用例数量、覆盖率、健康、E2E、UI Gate 与性能产物状态
4. **硬门禁失败**：L2/L3 缺失的集成、性能或其他强制证据

## 质量标准

### 覆盖率

业务代码覆盖率从 `--coverage-dir` 读取。任意语言可输出统一的 `harness-coverage.json`（`{"percent": 80.0}`）；同时兼容 coverage.py 的 `coverage.json` 与 TypeScript 的 `coverage-summary.json`。生成方式由 `config/harness.yml.commands.unit` 和项目测试配置决定，Harness 不绑定语言或包管理器。

业务模块（如 `src/services/`、`src/modules/`、`src/providers/`、`src/shared/`、`src/plugins/`、`src/routes/`、`web/src/`）：**statements ≥ 80%**

### 集成测试链路

从 PRD 主路径/异常路径 + 技术方案跨服务调用图推导，每条链路覆盖正常 + 主要异常路径。

脚本优先执行 `config/harness.yml.commands.integration`；未配置时仅将文件模式作为“存在集成测试”的信号，支持：
   - `*.integration.*`
   - `*.int.*`
   - `integration.test.ts` / `integration.spec.ts`
   - Python/TypeScript 项目约定的 integration/int 测试命名

### 性能基线

从技术方案 API 定义 + PRD 非功能性需求生成：

| 类型 | 参考上限 |
|------|---------|
| AI 推理接口 | P99 < 8s |
| SSR 渲染 | P99 < 500ms |
| 高频上报 | P99 < 200ms |
| 前端 LCP | < 2.5s（Web Vitals Good） |
| 前端 FCP | < 1.8s |
| 前端 CLS | < 0.1 |

### E2E 测试（**以 test-case 为质量度量单位**）

> **case vs spec 关系**：测试场景以 `docs/test-cases/**/*.yml` 为权威清单，
> Playwright spec (`tests/e2e/scenarios/*.spec.ts`) 仅是 case 的执行载体；
> 多个 case 可共用同一 spec，质量度量以 case 为准。
> spec 数量、spec 覆盖率仅作为底层运行/排障辅助信息，**不进入质量评分**。

E2E 评分基于 Playwright 执行测试用例的结果，按 **case** 归因：

**当前迭代 case**（全量执行）：
- 加载 `docs/test-cases/**/*.yml` 中 `introduced_in` 或 `last_modified_in` 指向当前 Sprint 的用例（legacy `sprint` 字段迁移期兼容）
- 对每个 case 执行其声明的 spec（按 `id` grep 锁定）
- 全部 case 通过得满分，任一 case 失败则本项 0 分

**真实链路 smoke**（按声明执行）：
- 若 case 声明 `execution.mode: live`，脚本会按 `execution.env` 额外执行对应 spec
- 所有 live case 全部通过，当前迭代 E2E 才视为通过
- `execution.env` 仅放非敏感运行开关；密钥继续来自环境变量

**P0 跨迭代回归**（全量执行）：
- 执行所有非当前迭代的 P0 case
- 全部通过为回归达标，任一失败则本项 0 分

当前迭代 + P0 回归 + 真实链路（若存在）均通过方可得 E2E 分数。

> **后续优化（已立项，本版本暂未落地）**：
> Step 5 当前对每个 case 单独触发一次 Playwright，相同 spec 会被启动多次；
> 工程化重构后将批量执行 spec 并通过 JSON reporter 按 case ID 归因，消除冗余。
> 当前实现保证正确性（每个 case 独立验证），仅在性能上有改进空间。

E2E 测试用例由 `test-case-gen` 任务生成，存放于 `docs/test-cases/` 目录。

### UI 还原度

UI 还原度满分 25，由 `harness quality-score` Step 6 自动评分。**判分依据是 prototype-parity 三项必须全部 PASS**：

| 子项 | 检查脚本 | 判据 |
|------|---------|------|
| 原型 100% 注册 | `uv run --project .harness/runtime harness check-prototype-coverage --sprint <id>` | `docs/design-docs/prototypes/sprint-<id>/*.html` 每个文件都被 `.harness/rules/ui-contracts.yml` 中某个 `contract.prototype.path` 引用 |
| 契约最低强度 | `uv run --project .harness/runtime harness check-contract-strength --sprint <id>` | 每个 contract 至少含 1 个 textList + 1 个 style + 1 个 metric + 1 个 presence/count，且总数 ≥ 6 |
| prototype-parity 全 PASS | `uv run --project .harness/runtime harness ui-audit --sprint <id>` | `coverage/ui-audit.json` `passed=true`，每个页面在浏览器渲染后所有 checks 全部通过 |

**评分**：三项全 PASS = 25/25；任一 FAIL = 0/25（无中间分）。

**侦察优先**：先 Playwright 截图确认页面实际结构，禁止盲猜 selector。

**截图有效性**：每张截图展示目标页面的实际内容（非重定向中间态、非白屏、非错误页）。

### 测试环境启动

E2E 测试前必须完成全栈环境启动。启动管理使用 `uv run --project .harness/runtime harness verify`。

## 达标阈值

- 总分 100 分，**≥ 95 达标**
- 退出码：0 = 达标，1 = 不达标

## 完成标准（DoD）

- 7 个评分维度全部计算（按 `project.type` 不适用的维度使用代码定义的 N/A 语义）
- 总分 ≥ 95
- 报告已保存至 `docs/test-reports/sprint-N-quality.md`
- 报告包含各维度实际检查结果；更细的截图、HTML report 和用例清单由对应测试产物保存并作为 Review artifact 绑定

---

## AI 执行协议

**允许工具**：bash（测试/服务启动/docker）、文件读取、`chrome-devtools`、`webapp-testing` | **禁止**：代码修改、文档修改

**执行步骤**：
1. 确定测试范围（Sprint 变更文件 + 影响分析）
2. 运行 `uv run --project .harness/runtime harness quality-score --sprint N-name` — 自动完成 7 维评分并生成报告
3. 如需 E2E/截图，先用 `uv run --project .harness/runtime harness verify health` 启动并验证服务
4. 验证报告内容完整（截图路径、覆盖率、E2E 列表、真实链路结果）
5. 若当前 Sprint 存在 `execution.mode: live` 用例，确认报告已记录 `真实链路: ✅/❌`
6. 生成报告 → ≥ 95 达标派生 `product-acceptance`；< 95 派生修复

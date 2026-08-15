# {项目名称}

> 本文件是 Agent 的知识地图（≤120 行）。框架区域由 `tools/agents_sync.py` 自动同步，
> 项目区域可自由编辑。修改框架区域无效——请到 mai-harness 修改 `templates/AGENTS.md`。

## 项目概述

<!-- TODO: 一句话描述项目 -->

**技术栈**：<!-- TODO: 如 Python/FastAPI + TypeScript/Vue + PostgreSQL + Redis -->
**部署形态**：<!-- TODO: Docker Compose / K8s / 云原生 -->
**部署环境**：见 `config/deploy.yml`

## 当前迭代

<!-- TODO: 指向当前活跃的 Sprint 文件 -->
- 活跃 Sprint：`docs/exec-plans/active/sprint-1-xxx.md`

<!-- harness:framework-map:start （由 tools/agents_sync.py 维护，请勿手工编辑） -->

## 快速命令

| 命令 | 说明 |
|------|------|
| `uv run --project .harness/runtime harness run-project-command <name>` | 执行 harness.yml 登记的项目命令；默认覆盖 Python 后端与 TS 前端 |
| `uv run --project .harness/runtime harness verify` | 端到端验证（health + 截图 + 日志 + 指标） |
| `uv run --project .harness/runtime harness doc-lint` | 文档健康（链接 + 索引 + 新鲜度） |
| `uv run --project .harness/runtime harness doc-garden` | 手动文档园艺（简明度 + 专业度 + 索引摘要） |
| `uv run --project .harness/runtime harness worktree create <id>` | 创建隔离工作空间 |
| `uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-plan-file> --task-id <task-id> --strict` | 任务前置条件校验 |
| `uv run --project .harness/runtime harness quality-score --sprint <id> --level L1` | 质量评分 |
| `uv run --project .harness/runtime harness promote test` | Develop → Test 提升 |
| `uv run --project .harness/runtime harness release init <vX.Y.Z>` | 创建 release 分支 |
| `uv run --project .harness/runtime harness deploy --env <test\|prod>` | 部署 |
| `uv run --project .harness/runtime harness lock check <env>` | 环境锁状态 |
| `uv run --project .harness/runtime harness env-check validate` | 环境登记 schema 校验 |

## Codex CLI 运行契约

- Codex 会自动读取本文件；不要依赖 `.agent/rules/*.md` 被自动加载。
- Codex 子 Agent 定义位于 `.codex/agents/*.toml`：`harness-plan`、`harness-exec`、`harness-review`。
- 当用户明确说“开始 Sprint / 规划迭代 / 执行 Sprint / deploy-sprint / 继续当前 Sprint”，或指向 `docs/exec-plans/active/*.md` 要求推进任务时，进入 Sprint 模式。
- 未进入 Sprint 模式的普通修复、解释、评审、单脚本任务由主线程直接执行；只有用户明确要求“使用子 Agent / 并行 Agent / spawn agents”时才派生 Codex subagents。
- Sprint 编排必须留在主线程：主线程先运行 Step 0，再按任务四步协议显式 spawn `harness-plan` → `harness-exec` → `harness-review`。
- 子 Agent 只接收边界上下文：任务 ID、任务类型、Sprint 计划路径、上游产物路径、上一步输出原文；不要把整库或整份 docs 粘给子 Agent。
- `codex exec` 自动化默认只读；需要改文件的脚本应显式使用 `--sandbox workspace-write`，不要使用已废弃的 `--full-auto` 作为默认路径。
- 网络、跨目录写入、生产部署、密钥读取等高风险操作必须让 Codex 走权限审批，不要要求绕过 sandbox。

## 文档导航

> Agent 按任务类型渐进式加载文档：先查索引，再只读取与当前需求强相关的正文，不要一次性全部读取。

| 我要做什么 | 先读 | 再读 |
|-----------|------|------|
| 规划迭代 / 拆分任务 | `.harness/docs/SPRINT.md` | `USER_STORIES.md` |
| 产品设计 / PRD | `.harness/docs/PRODUCT_SENSE.md` | `USER_STORIES.md` |
| UI 设计 | `.harness/docs/DESIGN.md` | `.harness/docs/UI_DESIGN_SYSTEM.md` + 关联 PRD |
| 后端技术方案 | `.harness/docs/TECH_BACKEND.md` | `ARCHITECTURE.md` |
| 前端技术方案 | `.harness/docs/TECH_FRONTEND.md` | `.harness/docs/UI_DESIGN_SYSTEM.md` + `ARCHITECTURE.md` |
| 后端编码 | `.harness/docs/CODING_BACKEND.md` | `PROJECT_RULES.md` + 技术方案 |
| 前端编码 | `.harness/docs/CODING_FRONTEND.md` | `.harness/docs/UI_DESIGN_SYSTEM.md` + `PROJECT_RULES.md` + 技术方案 |
| 代码评审 | `.harness/docs/CODE_REVIEW.md` | `PROJECT_RULES.md` |
| 测试质量评分 | `.harness/docs/QUALITY_SCORE.md` | `PROJECT_RULES.md` |
| 产品走查 | `.harness/docs/PRODUCT_ACCEPTANCE.md` | PRD + 设计文档 |
| **Sprint / deploy-sprint 流程** | **`.harness/docs/SPRINT.md`** | `.harness/rules/task-rules.yml` + `config/harness.yml` |
| **CI/CD 红线 / 分支模型** | **`.harness/docs/CICD.md`** | `config/deploy.yml` |
| deploy-sprint（test/prod） | `.harness/docs/SPRINT.md` | `.harness/docs/RELEASE.md` + `config/deploy.yml` |
| 数据库迁移 | `docs/MIGRATION.md` | `templates/migration/` |
| Secrets 管理 | `.harness/docs/SECRETS.md` | `config/deploy.yml` |
| 线上观测 | `.harness/docs/OBSERVABILITY.md` | `.harness/templates/observability/` |

## 知识库索引

```
docs/
├── product-specs/          # PRD（含 index.md 验证状态）
├── design-docs/            # UI 设计 + prototypes/
├── tech-docs/              # 技术方案
├── exec-plans/             # 迭代计划（active / completed）
├── review-reports/         # 评审
├── test-reports/           # 测试
├── acceptance-reports/     # 产品走查
├── observability-reports/  # 线上观测
├── bugs/                   # Bug 跟踪
└── references/             # 参考资料

config/harness.yml          # Harness 项目级行为配置（走查环境、质量阈值、部署模式）
config/deploy.yml           # 发布环境与部署配置
.harness/state/             # 环境锁、promotion 日志、框架运行状态
deploy/                     # test/prod 部署生成产物
templates/migration/        # DB migration 模板（脚本 readFileSync）
.harness/templates/observability/    # PromQL / LogQL 可执行查询
```

<!-- harness:framework-map:end -->

## 项目目录结构

<!-- TODO: 替换为实际目录结构 -->

```
├── src/                    # 后端源码
├── web/                    # 前端源码
├── docs/                   # 知识库（→ 见上方索引）
├── AGENTS.md               # 本文件
├── ARCHITECTURE.md         # 系统架构
├── PROJECT_RULES.md        # 项目编码规则
└── USER_STORIES.md         # 用户故事
```

## 智能体约定

- **沟通语言**：所有智能体（Agent）在与用户进行对话、工作状态反馈、报告编写时，**必须统一使用中文**进行交流（技术术语和代码标识符除外），以保证沟通的连续性。

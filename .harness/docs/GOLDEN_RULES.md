# 黄金原则（Golden Rules）

> 从 Bug 根因、评审 Major、重构经验中提炼的通用工程品味规则。
> 适用于所有 Harness 项目。成熟规则升级为 Lint（第1层前置阻断）。

## 原则列表

| # | 原则 | 检查方式 | Lint 规则 |
|---|------|---------|----------|
| G-1 | **复用先于复制，所有权先于共享**：先复用已有能力；只有稳定共同语义和真实消费者才提取共享抽象，业务语义留在拥有它的领域 | Lint 提示 + Code Review | `harness/no-duplicate-helper` |
| G-2 | **禁止 YOLO 探测数据**：所有外部输入必须边界校验或 SDK 强类型访问 | Lint（pre-commit + CI） | `no-explicit-any`（项目启用） |
| G-3 | **不变式集中管理**：魔法数字、业务常量、配置阈值集中到 `config/` 或 `shared/constants` | Lint（pre-commit + CI） | `harness/no-magic-values` |
| G-4 | **禁止 GPL 依赖**：不得引入 GPL/AGPL 依赖；Python 与 TypeScript 分别执行项目配置的许可证/漏洞审计 | 项目审计命令 / CI | — |
| G-5 | **E2E 场景覆盖关键链路**：fullstack/frontend 的用户可见链路须有 `tests/e2e/scenarios/` Playwright 用例；backend 使用 API 集成测试 | 当前 project.type 测试命令 + CI code-garden | — |
| G-6 | **副作用与重试语义必须明确**：存在网络重试、并发写或不可重复副作用时，按业务语义选择天然幂等、Idempotency Key、去重、锁、版本检查、补偿或明确拒绝重试；无风险证据时不默认引入 Redis 幂等层 | Code Review | — |
| G-7 | **外部链路保留真实证据**：涉及第三方能力、媒体持久化、支付、消息发送、AI 推理等用户可见外部链路时，测试用例至少 1 条声明 `execution.mode: live`，质量报告须记录真实链路结果。执行 live 用例时，缺失的真实 KEY / 账号通过 `ask_user` 向用户补充；仅当用例 `execution.mock_reason` 字段明确声明不可测原因（如第三方服务受可信域名 / 可信 IP 限制无法在本地 Docker 测试）时，允许使用 Mock 替代 | `quality_score.py` + 产品走查 | — |
| G-8 | **本地 Docker 统一运行**：本地联调、API/E2E/UI 还原度/产品走查统一基于 `docker compose` 容器（中间件 + API + Web + Mock），客户端程序在宿主机运行；细则见 [G-8 代码化要点](#g-8-代码化要点) | `verify.py` + `sprint_gate.py` | — |
| G-9 | **业务规则由明确的业务代码拥有**：默认将授权、状态迁移、业务校验、计算口径和流程编排保留在领域/应用代码；不通过存储过程、函数、触发器或数据库定时任务形成隐藏业务执行。数据库保留事务、声明式完整性约束和查询执行。数据库中心项目的既定外部约束必须在架构显式声明，不能被通用模板隐式引入或删除 | Lint + 规则所有权 Review | `harness data-lint` / G-9 |
| G-10 | **数据访问遵守声明的兼容边界**：项目必须声明数据库/方言版本、ORM 或 query builder 及允许/禁用能力；手写、ORM 生成、分析、迁移及自有运维 SQL 均遵守同一边界。领域不直接依赖厂商细节，原生 SQL 集中、参数化并附必要的真实数据库验证；ORM 类型或语法检查不能替代兼容证据 | Lint + 目标数据库集成测试 | `harness data-lint` / G-10 |
| G-11 | **业务 ID 是端到端契约**：架构明确生成算法、唯一性范围、存储与传输表示、跨语言精度和恢复语义；所有消费者保持无损一致。采用分布式整数发号时，验证节点冲突、时钟、重启和序列边界；外部与观测协议标识分别建模，不因共用 ID 名称混用 | Lint + 往返及生成器故障测试 | `harness data-lint` / G-11 |

<!-- 持续补充：Bug 根因 → 黄金原则 → code-garden 验证 → Lint 固化 -->

### G-8 代码化要点

- 项目通过 `docker-compose.yml` 声明所有容器（中间件 + API + Web + Mock）。
- `verify.config.sh` 配置：
  - `DOCKER_COMPOSE_FILE` — compose 文件路径
  - `DOCKER_BUILD_SERVICES` — 按源码重建的服务（通常 `api web`）
  - `APP_START_SERVICES` — `up -d --wait` 启动的服务
  - `MOCK_SERVICE_NAME` — Mock 服务名；纯后端 Sprint 可显式置 `NONE` opt-out
- **唯一启动入口** `verify.py docker-up`：`down → 按源码 build → up -d --wait`。
- **幂等 health** `verify.py health`：先检测目标服务是否已 healthy，是则跳过；否则委托 `docker-up`。
- **每任务前置** `sprint_gate.py` 自动调用 `verify.py preflight`，缓存 TTL 来源 `task-rules.yml.sprint_preflight.ttl_seconds`，关键文件 mtime 变更立即失效。
- **数据安全** `verify.py docker-down` 默认保留 volume；销毁数据需显式 `--purge`。

### G-9～G-11 适用边界与验证

- 框架统一规则所有权、兼容声明和无损契约；具体数据库、方言、ORM 和 ID 算法由项目 `ARCHITECTURE.md` / `PROJECT_RULES.md` 确定，不跨项目强制 TiDB、MySQL、Drizzle 或 Snowflake。没有数据库或自建业务 ID 的项目声明不适用，不为满足模板新增组件。
- 项目已明确的禁用能力属于硬约束，框架的默认适用性说明不能将其降为可选。既有外部数据库约束须在架构说明边界与责任，不得以“ORM 自动生成”或“运维脚本”绕过声明的 SQL 范围。
- 业务规则在代码，不等于把关系查询、聚合、事务或完整性约束全部搬入应用内存；规则的定义与编排必须有明确的代码所有者。
- 涉及数据或 ID 的设计、编码和评审分别从 [TECH_BACKEND.md](TECH_BACKEND.md)、[CODING_BACKEND.md](CODING_BACKEND.md)、[CODE_REVIEW.md](CODE_REVIEW.md) 加载本节，并按变更影响落实证据。数据库兼容验证覆盖实际生成语句、执行语义和适用的迁移；ID 验证覆盖持久化、接口、事件、队列及实际消费者。
- 静态门禁统一运行 `harness data-lint`，读取 `config/technology.yml#data_policy`；pre-commit 与 CI static 命令组调用同一入口。动态查询结果、目标数据库执行语义与分布式 ID 唯一性仍由对应契约/集成测试证明。

### G-9～G-11 静态 Lint

`uv run --project .harness/runtime harness data-lint [--json]` 是统一入口；违规、非法策略、解析失败或所需解析器缺失均非零退出，并返回文件、行号和规则编号。

| 规则 | 自动检查 |
|---|---|
| G-9 | 在 SQL 文件及 AST 提取的原生 SQL 中拒绝项目禁用的 PROCEDURE/FUNCTION/TRIGGER/EVENT 定义及存储过程调用；识别可执行注释，不扫描普通注释或字符串内的伪 SQL。 |
| G-10 | 校验数据库/方言/ORM 的版本声明，检查显式禁用的 SQL 函数和语法能力、直接拼接的动态 SQL；启用 Drizzle 限制时检查关系查询 `with`、常量别名/对象展开和无法静态展开的 options。普通字段 `with`、SQL CTE 和 `db.with()` 不命中关系加载规则。 |
| G-11 | 根据声明的业务 ID 字段模式检查 SQL 列类型/生成方式、TS 数值类型及直接 Number/parseInt/parseFloat/一元加号转换、Drizzle 数值映射/禁用生成器、TypeBox 和 JSON Schema 的数值 ID；外部与观测标识按显式排除列表区分。 |

策略字段由项目填写，不跨工程固定数据库或 ID 算法：

| `data_policy` 字段 | 内容 |
|---|---|
| `sources` | 工程内的源码、迁移、契约及导出的 ORM SQL 文件/目录；目录递归扫描，禁止路径或符号链接越界。文档、依赖包、构建目录不作为业务源文件。 |
| `database` | `engine/version/dialect/dialect_version/orm/orm_version` 六个非空字符串；无数据库可为 null。 |
| `forbidden_database_objects` | 从 `procedure/function/trigger/event` 选择项目禁用对象。 |
| `forbidden_sql_functions` | 禁用 SQL 函数名列表，忽略函数名大小写。 |
| `forbidden_sql_features` | 从 `fulltext/lateral/skip_locked/tidb_extensions/json_operators` 选择禁用能力。 |
| `forbid_drizzle_relations` | 是否禁止 Drizzle 关系加载；不能代替 SQL 能力列表。 |
| `business_ids` | `strategy/scope/storage_type/wire_type` 契约及 `fields/exclude_fields/forbidden_generators` 列表；字段支持 glob，外部/Trace/算法节点编号等按真实协议排除。无自建业务 ID 可为 null。 |

TS/JS 使用项目已安装的 TypeScript 5/6 Compiler API，Vue SFC 另使用项目的 `@vue/compiler-sfc`；Python SQL 使用标准库 AST，独立 SQL 使用分词器。未声明策略输出 `not-configured`，无适用源文件输出 `no-sources`，均不宣称业务代码已通过检查；涉及数据的项目必须登记策略，并将 `data_lint` 纳入自己的 precommit/static/CI 命令组，不能因覆盖默认组而漏掉检查。

这是有明确覆盖范围的静态门禁：不执行应用、SQL 或迁移，不声称完整校验所有方言语法、跨模块运行时封装、任意动态 SQL 或发号器唯一性；生成 SQL 样本应纳入 `sources`，运行行为在实现测试中验证。通用规则与 Lint 用法只在框架维护，项目 Architecture 保留具体选型与边界，不复制本表或规则映射。

## 执行层级

```
品味观察 → 黄金原则（本文档）→ Lint 前置阻断（pre-commit + CI）
```

| 层级 | 工具 | 时机 | 触发确定性 |
|------|------|------|-----------|
| 前置阻断 | `lint/harness-plugin.mjs` | pre-commit hook + CI workflow | ✅ 确定 |
| 模块健康 | `uv run --project .harness/runtime harness code-garden --ci` | CI workflow | ✅ 确定 |
| 文档定义 | 本文件 | Agent 设计/编码/评审时加载 | 适用规则必须核对；未代码化项由 Review 与验证证据检查 |

## 新增规则流程

1. 从 Bug 根因、评审 Major、重构痛点中识别模式
2. 编码为黄金原则（本文档新增行）
3. 接入适用的设计、编码与评审入口，明确检查范围和证据；尚不可机械判定的规则在表中标明未代码化
4. 对可可靠判定的部分新增语言适用的 Lint 或项目契约检查，并接入 CI/pre-commit；JS/TS 静态规则可落入 `harness-plugin.mjs`，需要模块级扫描时再接入 `code_garden.py`

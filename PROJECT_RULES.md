# 项目编码规则

> ⚠️ **MAI-Harness 框架文件** — 请勿在项目中修改。如需变更请在框架工程中修改并覆盖到此项目。

> 项目级的编码规则与约定。Agent 在编码和评审任务中加载本文件。
> 分层架构规则见 [ARCHITECTURE.md](ARCHITECTURE.md)。
> 框架级编码规范见 `docs/CODING_BACKEND.md` / `docs/CODING_FRONTEND.md`。

## 命名约定

| 对象 | 约定 | 示例 |
|------|------|------|
| 文件名（组件） | 项目前端约定 | `UserProfile.tsx` |
| 文件名（Python 模块） | snake_case | `user_profile.py` |
| 变量/函数 | Python snake_case；TypeScript camelCase | `get_user_by_id` / `getUserById` |
| 常量 | UPPER_SNAKE_CASE | `MAX_RETRY_COUNT` |
| 类型/接口 | PascalCase | `UserProfile` |
| 前端组合函数/Hook | 遵循项目框架约定 | `useAuth` |
| 路由路径 | kebab-case | `/user-profile` |
| 数据库表 | snake_case | `user_profiles` |
| CSS 类名 | kebab-case / BEM | `user-profile__avatar` |

<!-- TODO: 根据项目实际情况调整 -->

## 编码约束（Key Rules）

> 这些规则将被 Lint 机械化强制执行。违反即红灯。

### 通用

1. **禁止空 catch 块**：至少记录日志
2. **函数体 ≤ 50 行**：超出须拆分
3. **圈复杂度 ≤ 10**：每个函数
4. **禁止硬编码密钥**：所有密钥通过环境变量注入
5. **结构化日志**：使用 logger 库，禁止 `console.log`
6. **SQL 参数化**：禁止字符串拼接 SQL
7. **文件 ≤ 300 行**：超出须拆分模块

### 后端

8. **边界处校验数据形状**：后端 Controller 使用 Pydantic/架构指定模型，前端使用 TypeScript Schema 校验外部输入
9. **Service 不感知传输层**：禁止在 Service 中引用 HTTP Request/Response
10. **Repository 不含业务逻辑**：纯数据访问
11. **统一错误格式**：`{ code, message, requestId }`

### 前端

12. **组件单一职责**：一个文件一个组件
13. **禁止 Props 超过 7 个**：超出须使用组合模式或上下文
14. **遵循 ARCHITECTURE.md 的渲染模式**：不得假设特定前端框架或 Server Component
15. **禁止 any 类型**：使用 unknown + 类型收窄

<!-- TODO: 根据项目实际情况调整 -->

## Pre-commit Checklist

> Agent 提交代码前必须逐项确认。

- [ ] `config/harness.yml` 对当前 stack 声明的 lint/typecheck/build 命令通过
- [ ] `command_groups.static` 全部通过（默认 Ruff/AST lint + ESLint/tsc）
- [ ] `commands.unit` 全部通过（Python 默认 pytest）
- [ ] 新增代码有对应测试
- [ ] 无硬编码密钥或敏感信息
- [ ] 符合分层架构规则（见 ARCHITECTURE.md）
- [ ] 无 `console.log`（使用结构化日志）
- [ ] 新增/变更的 API 有 Pydantic/架构指定的请求响应 Schema 校验
- [ ] 数据库变更通过迁移文件（禁止手动修改）
- [ ] 相关文档已同步更新

<!-- TODO: 根据项目实际情况调整 -->

## 代码质量基线

| 指标 | 阈值 | 说明 |
|------|------|------|
| 测试覆盖率 | ≥ 80% | 关键路径 100% |
| 构建时间 | ≤ 60s | CI 构建 |
| Bundle 大小 | ≤ 200KB | 首屏 JS（gzipped） |
| 慢查询 | ≤ 200ms | 数据库查询 |
| API 响应 | ≤ 500ms | P95 |
| Lighthouse | ≥ 90 | Performance 得分 |

<!-- TODO: 根据项目实际情况调整 -->

## 黄金原则（Golden Rules）

> 跨项目通用工程原则，详见 [docs/GOLDEN_RULES.md](docs/GOLDEN_RULES.md)（symlink 自动同步）。
> G-1（共享工具库）、G-2（禁止 YOLO 探测）、G-3（不变式集中管理）。

## 共享工具库

> 黄金原则 G-1：优先使用共享工具库，禁止手写重复 helper。

| 工具 | 路径 | 说明 |
|------|------|------|
| <!-- TODO --> | `src/shared/` | <!-- TODO --> |

## 禁止事项

- ❌ 禁止绕过项目选定前端框架直接操作 DOM
- ❌ 禁止使用 `eval`、`Function()` 等动态执行
- ❌ 禁止在循环中进行数据库/API 调用（使用批量操作）
- ❌ 禁止 YOLO 式探测数据——见 GOLDEN_RULES.md §G-2
- ❌ 禁止跨层直接引用（见 ARCHITECTURE.md 依赖方向规则）
- ❌ 禁止魔法数字/硬编码业务常量——见 GOLDEN_RULES.md §G-3

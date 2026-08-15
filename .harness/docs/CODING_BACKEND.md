# 后端编码规范

> 后端 `code` 任务的编码约束与完成标准。专用于 `code` 任务类型（后端）。
> 技术方案见 [TECH_BACKEND.md](TECH_BACKEND.md)，线上观测见 [OBSERVABILITY.md](OBSERVABILITY.md)。

---

## 上下文边界

Coding 只读取本次迭代 PRD、设计、技术方案与项目编码规范。不得回读历史 PRD/设计/技术方案作为实现依据；历史变更、优化、删除必须已经沉淀在本次迭代技术方案中。

---

## 可靠性

### 错误处理

- **禁止空 catch**，至少记录日志
- 统一响应格式：`{ code: string, message: string, requestId: string }`
- 错误码定义于项目统一 Python 模块，使用项目异常基类携带 `code` 与安全消息
- 禁止暴露 stack trace

### 日志

- 生产级别：`info`；每条含 `requestId`（链路追踪）；敏感字段脱敏

### 重试 / 幂等 / 并发

- 外部 API：timeout + 指数退避（retries: 3, factor: 2, 500-5000ms）
- 写操作：`Idempotency-Key` Header，Redis 缓存 24h
- 高并发链路：Redis 队列异步化，热路径无重复 IO

---

## 安全

| 领域 | 要求 |
|------|------|
| 认证 | JWT（HttpOnly Cookie）、Access 15min / Refresh 7d、RBAC owner/member/viewer |
| 输入验证 | 路由层 Pydantic Schema（或架构指定的等价模型）、URL 防 SSRF、文件上传校验 MIME+大小 |
| 数据安全 | ORM/驱动参数化查询（禁止拼 SQL）、密码使用当前安全哈希基线、敏感字段脱敏 |
| 接口安全 | Rate Limiting、CORS 白名单（禁通配符）、框架安全 Header 中间件 |
| 密钥 | 禁止提交代码仓库、仅提交 `.env.example`、使用项目声明的 Secret Manager、依赖漏洞扫描无 High/Critical |

涉及认证/支付/访问控制变更须安全专项审查。

---

## 完成标准（DoD）

- `config/harness.yml` 中 Python lint、format、typecheck、unit/integration 命令全部通过，覆盖率 ≥ 80%
- `uv run --project .harness/runtime harness verify health` 通过（API 返回 200）
- fullstack 项目中涉及用户可见链路的变更须有 `tests/e2e/scenarios/` 下对应的 Playwright 场景；python-backend 使用 API 集成测试
- 新增/变更 API 已更新文档、`.env.example` 已更新、DB 迁移文件已新增
- 技术债记录到 `tech-debt-tracker.md`

---

## AI 执行协议

**允许工具**：文件读写/搜索、bash（build/test/lint）、子代理、Python 后端最佳实践 | **禁止**：修改规范文档

**代码生成约束清单**：
- 错误处理：无空 except + 统一错误码 + 日志含 requestId + 敏感脱敏
- 重试/幂等/并发：外部 API 指数退避 + 写操作 Idempotency-Key + 高并发异步队列
- 安全：认证授权 + Pydantic/边界模型校验 + 参数化查询 + Rate Limiting + CORS 白名单 + 无密钥硬编码

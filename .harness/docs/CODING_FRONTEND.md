# 前端编码规范

> 前端 `code` 任务的编码约束与完成标准。专用于 `code` 任务类型（前端）。
> 技术方案见 [TECH_FRONTEND.md](TECH_FRONTEND.md)。

---

## 上下文边界

存在适用前端技术方案时，Coding 的直接派生输入只有 `task-context.upstream_inputs` 投影出的本轮前端技术方案；PRD/设计是技术方案的上游，不再与技术方案并列指导实现。USER_STORIES 摘要仍用于漂移门禁，但 `requirements.access=integrity-only` 时不得直接回读需求补充实现。不得回读历史 PRD/设计/技术方案；历史变更、优化、删除必须已经沉淀在本轮技术方案中。流程明确不产生技术方案时，只使用 task-context 的显式直接输入和项目编码基线，不自行检索或补造设计。

---

## 完成标准（DoD）

**通用**：`config/technology.yml` 声明的前端必需命令全部通过，覆盖率 ≥ 80%，技术债记录到 `tech-debt-tracker.md`

**前端附加**：
- **浏览器联调必须执行**：使用 `webapp-testing` 或 `chrome-devtools` 验证，禁止 curl/单测替代
- **联调截图交付**：每个任务产出浏览器截图证明渲染和交互正常
- **E2E 场景用例**：新增/变更的业务链路须有项目 E2E runner 可执行的场景用例
- **侦察优先**：先截图确认实际 DOM，再写测试断言，禁止盲猜 selector
- **修改现有优先**：新增文件须在技术方案中说明理由
- **原型保真**：实现保留原型的容器/标题区/卡片/气泡的结构与层级；原型示例的所有内容类型/状态在实现端可被看到
- **契约由代码守护**：`uv run --project .harness/runtime harness check-prototype-coverage --sprint <id>`、`uv run --project .harness/runtime harness check-contract-strength --sprint <id>`、`uv run --project .harness/runtime harness ui-audit --sprint <id>` 全部 PASS

---

## 安全约束

- 富文本和外部输入使用当前 technology Profile 声明的安全/Schema 能力；客户端校验不替代服务端信任边界
- 禁止前端硬编码 API Key/Secret；认证凭据存储和传递服从 Architecture，不假定固定 Token 形态

---

## 前后端联调

**前置**：后端 API 完成 + 文档更新 + 服务可启动

**流程**：启动后端 → 启动前端 → `uv run --project .harness/runtime harness verify health screenshot` → `chrome-devtools`/`webapp-testing` 补充验证 → 问题即修

**验证清单**：DB 迁移完成 + 后端健康 + 前端渲染正常 + API 调用正确 + 状态切换正确 + 表单闭环 + Console 无异常 + 主路径 E2E 走通

**产出物**：Playwright 截图 + DOM 侦察结果 + 验证清单（缺截图 → AI REVIEW 标记 `incomplete`）

---

## AI 执行协议

**允许工具**：文件读写/搜索、bash（build/test/lint）、子代理、`vue-best-practices`、`chrome-devtools`、`webapp-testing` | **禁止**：修改规范文档

**代码生成约束清单**：
- 框架语法、组件模式、路由、状态库和命名与 `config/technology.yml` 的当前 Profile 一致
- 状态具有单一真源和最近拥有者，可推导状态未重复存储，服务端状态未复制为长期客户端真源
- API 调用复用 Architecture 声明的数据访问/认证/错误边界，不由组件建立旁路
- 共享组件具有技术方案登记的真实消费者与稳定语义，不新增万能配置组件
- 性能、响应式、埋点和状态机只实现技术方案已触发内容，并提供对应浏览器证据
- 安全：按真实输入/展示边界处理，无硬编码密钥
- 联调产出物完整

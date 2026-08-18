# Vine 架构

> Vine 是嵌入受管业务客户端的 Vue 3 H5 终端工作流界面，不是通用管理端或权限服务。

## 1. 定位与边界

Vine 加载 Stem 签发、Celt 转交的 Todo 人机步骤，呈现表单、预览和确认并通过版本化 Celt Bridge 请求受控能力、回传结果。Workflow/分支/失败/子流程由 Stem 控制，Action/Operation 由 Celt 执行；Vine 不定义业务工作流、不持有 Secret、不决定权限或最终状态。

Vine 不依赖 Python 后端公共库 Seed，也不向 Seed 提交 Dependency Assignment。前端公共能力留在 Vine 自身前端基础层或 Celt Bridge/服务 OpenAPI 契约中，不通过 Python Wheel 或 Seed adapter 间接引入。

## 2. 结构与契约

`transport` 负责消费 Celt 发布的 Bridge 握手/消息 Schema；`runtime` 负责上下文、协议兼容和恢复；`features` 只负责有类型的人机 Step renderer；`components` 只含 UI；`telemetry` 产生脱敏行为事件。Vine 不拥有 Bridge wire contract，也不手写协议 DTO。上下文固定 `tenant/task/todo/execution/attempt/fencing/terminal/action/workflow` 版本、目标/参数摘要、幂等键、允许能力、Vine digest、协议版本、过期时间和 Stem 签名。

每个 HumanStepDescriptor 包含全局 `step_code`、renderer version、input/output JSON Schema、字段约束与脱敏级别、只读影响摘要、风险提示、允许 command（edit/accept/reject/ack/cancel）和本地校验规则。Vine 只能加载自身 manifest 登记且与协议兼容的 renderer；未知 step_code 或 Schema 拒绝执行，不以动态 HTML/脚本扩展。

Bridge 启动挑战生成页面实例 nonce，并绑定 Celt 进程、来源窗口、Todo/Execution attempt 和单调双向 sequence；请求/响应均有关联 ID 与 MAC/签名，重放、错误来源和 allowlist 外方法默认拒绝。Celt 重新验证租约/fencing 和能力，页面 allowlist 不是最终授权。

Vine 操作前必须建立短期 InteractionSession：当前操作者经 OIDC/device authorization 登录后，由平台签名绑定 tenant/user/account/terminal/Todo/attempt；Celt 同时核对当前业务账号。共享 CVD 的身份/账号不匹配、会话过期或页面遗留时清空内容并重新认证，回执携带实际 interaction principal。

每个回执严格按 Celt Bridge Schema 包含 Task/Todo/Execution/attempt、Action/Workflow 版本、step ID、attempt 内 sequence、幂等键、起止时间、输入摘要、结构化 output、error envelope 和 evidence refs。Vine 只报告用户交互结果；Celt/Stem 转换为权威 Step/Execution 状态。

平台高风险 Confirmation 必须在 Stem 入队前完成，Vine 不授予权限。Vine 的 `ack` 只是已授权 Todo 在终端侧的即时安全确认，不能改变目标/参数/版本，不能替代或创建 Confirmation。取消/暂停/租约撤销由 Celt 推送并要求 ACK：未调用 Bridge 的步骤立即停止；进行中的可取消 Operation 在安全点停止；已产生但结果未知的副作用上报 uncertain，由 Stem 裁决，页面不得自行重试。草稿加密并绑定 tenant/user/account/Todo/attempt/Schema/Vine digest；成功、取消、撤销、过期、登出或身份切换时清除。

### 2.1 业务组件

| 模块 | 职责 |
|---|---|
| Bootstrap | runtime config、manifest、CSP、版本校验 |
| Interaction Session | 操作者认证、账号/终端/Todo 绑定与过期 |
| Bridge Transport | challenge、sequence、request/response correlation、ACK |
| Step Runtime | descriptor 校验、renderer 选择、草稿和 command |
| Renderers | form/preview/choice/ack/progress 等纯交互组件 |
| Recovery | 重连、草稿销毁、取消/租约事件收敛 |
| Telemetry | 脱敏行为、性能和错误，不采集业务正文 |

### 2.2 核心流程与非功能

`load signed context → authenticate operator → bridge challenge → select renderer → validate input → local safety ack if declared → bridge command → correlate result → submit ordered receipt → clear draft`。任何版本、身份、lease、sequence 失败都 fail closed。

- 扩展：新增 HumanStep 只增加 descriptor + renderer，不复制 Bridge/runtime；远端动态脚本禁止。
- 性能：首屏 shell 小型化，renderer 按签名 chunk 加载；断网草稿有大小/TTL；Bridge 调用有 deadline。
- 安全：CSP/Trusted Types/SRI、无 Secret、InteractionSession、敏感字段不进 telemetry/localStorage。
- 可维护：Step renderer 无服务 API，只调用 typed Bridge；状态机集中在 runtime。

产品路线：V0 Bridge shell → V1 通用表单/确认 → V2 Reply renderer → V3 Broadcast/Group renderer → V4 可访问性/多语言/弱网 → V5 经审查的 renderer SDK。

## 3. 发布与验证

Vine 的唯一发布模型是不可变静态制品封装 OCI 镜像，经 Helm/Ingress 发布；Celt 在受控 WebView 加载 manifest 固定的 HTTPS URL/digest，不捆绑第二份 Vine。Release Manifest 联合固定 Vine/Celt/Bridge/Stem 协议版本；CSP、来源 allowlist、依赖锁和资源完整性启用。Test 在真实 Celt Bridge 覆盖握手/伪造来源、签名失败、未知方法、重连、版本/租约撤销、取消竞态、乱序/重复回执、跨身份草稿、脱敏和无障碍。

`maia-vine-v0.1.0` 切换签名 HumanStepDescriptor、InteractionSession、typed Bridge 和有序回执，并迁移已登记 renderer；Test 联合 Celt/Stem 验证后，`maia-vine-v0.2.0` 删除旧 H5 流程、未签名上下文、动态脚本/HTML loader 和旧 Bridge 方法，不保留运行时兼容分支。

# Vine 用户故事

> 演进顺序：V0 Bridge → V1 通用 renderer → V2 Reply → V3 Broadcast/Group → V4 弱网/可访问性。Vine 只负责人机 Step，不定义 Workflow 或授权。

| ID | 用户故事 | 验收标准 | 来源 | 状态 |
|---|---|---|---|---|
| VINE-001 | 作为终端用户，我希望安全加载 Todo 工作流。 | 验证签名/租约/版本；仅显示允许步骤；无效上下文阻止执行。 | CEL-005 | `draft` |
| VINE-002 | 作为 Celt，我希望与 H5 完成版本化 Bridge 握手。 | 消费 Celt 发布的固定 Bridge Schema/生成类型；能力协商、超时、错误和重连明确；未知方法默认拒绝。 | CELT-029/CEL-005 | `draft` |
| VINE-003 | 作为用户，我希望完成人机回复步骤。 | 上下文最小化；编辑/接受/拒绝/取消可用；结果转为标准步骤回执。 | TEA-004/CEL-005 | `draft` |
| VINE-004 | 作为用户，我希望完成广播和群管理人机步骤。 | 目标/影响可见；只提供不授予权限的终端安全 ack；平台 Confirmation 已在入队前完成；重复提交无重复副作用。 | MNT-001~003/CEL-005/CFG-006 | `draft` |
| VINE-005 | 作为用户，我希望断线后安全恢复。 | 草稿绑定 Todo；重连核对租约和序号；过期内容不再执行。 | CEL-001 | `draft` |
| VINE-006 | 作为发布负责人，我希望验证 Celt/Vine 兼容性。 | 兼容矩阵和消费者契约通过；不兼容版本阻止执行；真实 CVD E2E 通过。 | CEL-005/FND-021 | `draft` |

## 产品化细化故事

| ID | 用户故事 | 验收标准 | 来源 | 状态 |
|---|---|---|---|---|
| VINE-007 | 作为平台，我希望未知 HumanStep 安全失败。 | descriptor/renderer/Schema 不匹配 fail closed；不加载动态脚本；错误可恢复。 | CFG-002/CEL-005 | `draft` |
| VINE-008 | 作为共享 CVD 用户，我希望页面确认当前操作者。 | InteractionSession 绑定 user/account/terminal/Todo；切换或过期重登；回执含实际主体。 | FND-004/CEL-005 | `draft` |
| VINE-009 | 作为用户，我希望区分平台确认和本地安全 ack。 | Confirmation 入队前完成；ack 不授权/不换参；二者审计标签不同。 | CFG-006 | `draft` |
| VINE-010 | 作为用户，我希望输入在断网后安全恢复。 | 草稿加密绑定身份/attempt/schema/digest；TTL/大小限制；成功/取消/登出清除。 | CEL-001/005 | `draft` |
| VINE-011 | 作为 Celt，我希望 Bridge 消息不可重放。 | challenge/nonce/双向 sequence/correlation/MAC；旧页面、乱序和伪造来源拒绝。 | CELT-029/CEL-005 | `draft` |
| VINE-012 | 作为无障碍用户，我希望所有 renderer 可键盘和读屏操作。 | 焦点/标签/错误提示/对比度符合基线；不以颜色单独表达风险；E2E 覆盖。 | CEL-005 | `draft` |
| VINE-013 | 作为发布负责人，我希望 Vine digest 可复现。 | Release Manifest 固定 OCI/Vine/Celt/Bridge；CSP/SRI；回滚不加载浮动资源。 | FND-021 | `draft` |

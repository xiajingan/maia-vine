# Vine 架构

**当前 Profile**：`simple-layered`

Vine 是 Celt 受控 WebView 中的 Vue 3 H5 人机步骤载体。产品入口见 [Vine 概览](../docs/product/vine/overview.md)，采用条件、协议归属及恢复语义见[终端人机交互契约](../docs/architecture/terminal-interaction.md#2-bridge-与受控上下文)。

## 1. 定位与采用条件

只有总仓确认了具体终端人机场景及验收，才采用所需 renderer/表单/预览能力。当前会话托管审核在 Sage，后续 Iris 操作同一 Tea 稿，不新增 Vine 审核或每个 Todo 必点一次的本地关卡。

Vine 不编排 Workflow、不持有 Secret、不裁决业务权限或最终状态，也不依赖 Seed 服务端包。下述模块组织服务于实际获准步骤，不要求先建设全部组件。

## 2. 本地结构与契约适配

`transport → runtime → features/renderers → components`；telemetry 只产生允许的脱敏行为和性能/错误。transport 固定消费 Celt 发布的 Bridge Schema/生成类型，不手写第二套 wire DTO；runtime 消费受控上下文和租约/版本事件；renderer 不直调服务 API，只用 typed Bridge。

本地 HumanStepDescriptor 关联 `step_code`、renderer version、输入输出 Schema、字段与脱敏规则、只读影响摘要、风险提示及所需局部 command。只加载制品 manifest 登记且兼容的 renderer，未知类型失败关闭；不以远端动态 HTML/脚本扩展。descriptor 的网络字段来自 Celt 契约，不能由 Vine 声明业务能力或权限。

来源窗口、页面实例、nonce、序号和关联由 transport 按[受控上下文与 Bridge](../docs/architecture/terminal-interaction.md#2-bridge-与受控上下文)验证；错误来源、重放、未知版本/方法及超时不给业务结果。实际交互人和账号须有效，若场景需要 InteractionSession，它只承接短期技术上下文；认证入口在实际采用时设计，不预设必须新增 OIDC/device authorization 登录流程。

交互回执仅报告用户输入/选择和必要步骤关联，经 Celt/Stem 转成权威执行事实。局部 safety ack、平台 Confirmation、Tea 审核及协议 ACK 分别处理，见[交互结果与恢复](../docs/architecture/terminal-interaction.md#3-交互结果确认与恢复)；页面不能自造确认、改目标或自动重发未知副作用。

### 2.1 组件职责

| 模块 | 本地职责 |
|---|---|
| Bootstrap | 公共 runtime config、manifest、CSP 和兼容校验 |
| Context / Session adapter | 当前交互人、账号/终端/attempt 绑定和失效；按实际场景采用 |
| Bridge Transport | challenge、sequence、correlation、ACK、超时与拒绝 |
| Step Runtime | descriptor 校验、renderer 选择、局部输入及命令关联 |
| Renderers | 所选 form/preview/choice/ack/progress 的纯交互组件 |
| Recovery | 重连重验、可编辑输入清理和待回执关联 |
| Telemetry | 脱敏行为、性能与安全错误，不采集业务正文 |

### 2.2 输入、恢复与质量

本地流程为 `加载受控上下文 → 核验当前交互身份/账号 → Bridge 握手 → 选择已登记 renderer → 校验输入 → 按声明局部应答 → 关联交互回执`；任何身份、版本、租约或 sequence 无效均失败关闭，不能推进步骤。

可编辑输入按根/User/账号/Todo/attempt/Schema/制品摘要隔离；允许持久化的输入受加密、大小和 TTL 约束。待回传事实由 Celt 独立持久化，输入清理不能删除已发生事实。恢复、清除和最小显示规则引用[数据保护](../docs/product/constraints/data-protection.md)及终端契约，不将输入草稿等同任务草稿，也不默认所有输入都可恢复。

renderer 按制品固定 chunk 加载，shell 保持小型；Bridge 有 deadline。浏览器基础层采用 CSP/Trusted Types/SRI、来源约束和输出编码，不将 Secret 或敏感字段放入 telemetry/localStorage。所采用步骤的键盘、读屏、焦点和错误呈现沿[全局验收](../docs/product/constraints/acceptance.md#24-全局验收约束)。

## 3. 制品与验证

静态制品、固定 URL/digest、发布与运行资源见[部署设计](../docs/DEPLOYMENT.md#6-制品发布与终端)；组合固定 Vine/Celt/Bridge/Stem 版本和消费者契约。采用步骤后按[组合交付验证](../docs/product/constraints/delivery.md#51-终端人机步骤的组合验证)在真实 Windows/WebView 验证握手、伪造来源、未知方法/版本、重复乱序、租约失效、身份切换、取消竞争、输入清理与无障碍，不以文档或 DOM 属性声称实际通过。

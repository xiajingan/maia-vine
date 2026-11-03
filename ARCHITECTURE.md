# Vine 架构

**当前 Profile**：`simple-layered`

Vine 是 Maia 的 Vue 3 H5 载体，支持 Celt 受控 WebView 与渠道应用侧边栏两类宿主。产品入口见[Vine 概览](../docs/product/vine/overview.md)，渠道能力待验证项见[企微接入约束](../docs/product/constraints/wecom-integration.md#4-平台待验证项)。

## 1. 定位与采用条件

渠道侧边栏用于渠道应用提供的客户/群界面锚点及获准交互，具体宿主、身份上下文和 CUA 读取能力尚待验证，不能声称企微已提供任意稳定定位接口。Celt WebView 只在具体人机步骤获准时采用。Tea 审核仍在 Sage，后续 Iris 操作同一稿，不新增 Vine 审核或每个 Todo 必点一次的关卡。

Vine 不编排 Workflow、不持有 Secret、不裁决业务权限或最终状态，不依赖 Seed 服务端包。

## 2. 本地结构与契约适配

本地按 `host transport → runtime → features/renderers → components` 组织；遥测只记录允许的脱敏行为、性能和安全错误。宿主适配分开，不能把所有宿主强制接入 Celt Bridge。

| 宿主 | 通信与可信上下文 | 边界 |
|---|---|---|
| Celt WebView | Celt 发布固定 Bridge Schema 和生成类型，绑定 User、实例、账号、终端、Execution/attempt、租约和页面来源 | renderer 只经 typed Bridge，不直连业务 API；来源、序号、重放和回执沿[终端契约](../docs/architecture/terminal-interaction.md#2-bridge-与受控上下文) |
| 渠道侧边栏 H5 | Mud 负责核验平台授权上下文，平台服务提供经认证接口；具体宿主协议/SDK 和稳定目标依据须验证 | 页面自报 corp/user/customer/group 不能作为权限；不默认存在 Celt Bridge，不返回服务商秘密或企业 token |

CUA 使用侧边栏锚点时，Celt 须把看到的实例、当前企微账号及目标与 Stem 固定工作核对。界面名称、群名和人数只是辅助，无法建立可信关联时拒绝副作用；页面本身不能派发任务或替代租约。

### 2.1 组件职责

| 模块 | 本地职责 |
|---|---|
| Bootstrap | 固定制品、公共配置、完整性及宿主兼容检查 |
| Host adapter | 分别处理 Celt Bridge 和已验证渠道上下文，不共用秘密或身份假设 |
| Context / Runtime | 当前 User、实例、账号、目标、步骤版本及失效 |
| Renderers | 只加载 manifest 登记且兼容的呈现/输入组件，不加载任意动态代码 |
| Recovery | 按原身份、实例、版本恢复合法输入，拒绝旧上下文 |
| Telemetry | 脱敏观测，不采集聊天正文或平台凭据 |

### 2.2 输入、恢复与质量

Celt 路径遵守页面来源/nonce/sequence/租约核验；渠道路径遵守验证后的宿主身份和服务器授权，不能套用未签发的执行上下文。切换人、实例或目标、失权及过期时清理对应敏感输入。

可编辑输入与必要持久回执分开；具体恢复遵守[数据保护](../docs/product/constraints/data-protection.md)，不等于 Mint 业务草稿或任意离线缓存。按固定制品采用 CSP、来源约束和输出编码，实际宿主支持情况须验证；键盘、焦点和读屏要求按采用场景验收。

## 3. 制品与验证

静态制品和运行配置见[部署设计](../docs/DEPLOYMENT.md#6-制品发布与终端)。Celt 组合验证真实 Windows/WebView/Bridge/Stem；渠道侧边栏另验真实企微宿主、实例身份、客户/群上下文、锚点核对、跨实例拒绝及过期恢复。没有真实能力依据前保留待验证，不以网页 mock、签名有效或 DOM 属性宣称 CUA 成功。

# Vine 工程规则

本工程遵守[总仓项目规则](../PROJECT_RULES.md)；本文只补充本工程适用差异。技术栈为 TypeScript / Vue 3 / Vite；Node.js 用于构建工具链，版本及边界以[本工程架构](ARCHITECTURE.md)和[技术栈基线](../docs/architecture/technology-stack.md)为准。

## 本工程约束

- transport 固定消费 Celt 发布的 Bridge Schema/类型，renderer 只用 typed Bridge、不直调业务 API；本地组件不另定义 wire DTO、权限或执行状态。
- 人机步骤及 InteractionSession 只按[实际采用条件](../docs/architecture/terminal-interaction.md#2-bridge-与受控上下文)建设，不预设新增登录流程或会话审核入口。
- 所采用 renderer 验证身份/attempt 隔离、输入清理与回执恢复，以及真实 Windows/WebView 的键盘/读屏路径；静态制品不得含秘密，预算按实际组合验证。

## 检查要求

当前文档变更须检查结构、链接/锚点、语义及 diff。实现建立后，本工程在真实清单/CI 中登记终端 H5 组件与 Bridge 适配所需的静态、行为、契约检查及必要的环境验证。只运行已配置且适用于本次变更的检查，不使用占位命令，不宣称尚未验证的门槛已通过。

# maia-vine

Celt 受控 WebView 中的 H5 人机步骤载体；按实际获准场景采用。

技术栈：TypeScript / Vue 3 / Vite；Node.js 用于构建工具链；具体版本见[技术栈基线](../docs/architecture/technology-stack.md)。

当前处于需求与设计评审阶段，无运行中的新业务实现；阶段和流程入口以[总仓状态](../AGENTS.md#当前状态)为准。

Vine 人机步骤只随总仓确认的具体场景采用，当前会话托管审核在 Sage。

## 阅读入口

- [本工程架构](ARCHITECTURE.md)：结构、数据/模块职责及边界。
- [工程职责与需求](../docs/product/vine/overview.md)：先看当前采用范围，再按[主故事索引](../docs/product/index.md)与[故事地图](../docs/product/story-map.md)选取业务。
- [公共规则](../PROJECT_RULES.md)与[工程规则](PROJECT_RULES.md)：实施及检查要求；[AGENTS](AGENTS.md)提供任务导航。
- [总仓架构分篇](../ARCHITECTURE.md#2-架构分篇索引)和[部署设计](../docs/DEPLOYMENT.md)：跨工程契约、运行及发布配置。

`docs/tech/` 保存当前模块设计，`docs/test-cases/` 保存本工程用例；普通文档维护按总仓规则检查并以 Git 追溯，明确启动的业务或修复流程才维护必要过程记录。实际运行命令在业务实现建立后由本工程真实清单和 CI 登记，不以当前文档代表已可启动或验收通过。

# Managed Harness 工程导航

本工程自主维护 `USER_STORIES.md`、`ARCHITECTURE.md`、Story、Sprint、代码和组件质量。
Assignment 是写入 `docs/assignments/inbox/` 的统一外部目标输入，可来自 Control 的产品/架构输入或其他工程的依赖能力输入。仅在用户通过 Chat 主动开始需求或 Sprint 规划时，由 `harness-managed` 扫描并明确接受、调整、延期或拒绝；接受后映射到本地 `USER_STORIES.md` 和 Sprint，再执行标准 Plan → Exec → Review。`assignment` CLI 是 Skill 的内部端口，不要求用户直接调用；不存在远程触发或后台监听。

若本工程使用 `project.type: library`，只运行 `library-sprint`；若消费者当前 Sprint 显式包含 `dependency-change`，可通过已登记的 coordinated dependency session 同步驱动 Library Sprint。同步 session 不经过 Assignment inbox，异步协作仍只走 Assignment。

共享 Test/Production 部署、系统 E2E、提升与回滚归 Control。本工程完成质量门禁和不可变制品构建后，由 `harness-managed` 调用内部发布端口，将 Delivery Manifest 留存在 `docs/deliveries/`，供 Control 后续主动读取。

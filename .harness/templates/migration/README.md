# Migration 模板

复制 `up.sql.tpl` / `down.sql.tpl` 到 `deploy/release/<vX.Y.Z>/migrations/`，
按序号 + 短描述命名（如 `001-add-user-deleted-at.up.sql`）。

详见 `docs/MIGRATION.md`。

生产迁移必须先用 `harness migration-progress begin` 固定 candidate，再按数据批次提交
checkpoint；迁移、质量检查、服务启动或发布失败时记录 `fail`，修复后 `resume`，
不得自动回滚业务版本、执行 down 脚本、重建候选数据或清除已提交进度；只有人员
明确要求发布回滚并生成 ask_user authorization 后才允许回滚。

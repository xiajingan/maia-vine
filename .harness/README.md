# Harness 私有命名空间

> Harness 受管运行时、规则、配置、模板和运行状态统一位于此目录，避免占用项目通用顶层目录。

## 子目录

| 目录 | 写入者 | 说明 |
|------|--------|------|
| `runtime/` | 安装器 | 独立 Python 3.12 环境、CLI、公共库与框架测试 |
| `config/` | 安装器 + 项目维护者 | 默认策略及项目执行配置 |
| `rules/` | 安装器 + 项目维护者 | Sprint、走查与 UI 契约 |
| `eslint/` | 安装器 | TypeScript 前端共享规则宿主 |
| `docs/` | 安装器 | Harness 规范文档 |
| `templates/` | 安装器 | 文档、部署、观测和 UI 生成模板 |
| `secrets/` | 人工填写 + `promote_prep.py` 首次生成模板 | `test.sh` / `prod.sh` 等本地运行时密钥；默认不入库 |
| `state/` | `lock.py` / `promote.py` / `acceptance_record.py` | 环境锁、提升日志、走查审批记录、版本戳 |
| `images/` | `build_image.py` / `build_artifact.py` | 本地镜像 tar / 构建产物（默认不入库） |
| `reports/` | `quality_score.py` / `verify.py` | 任务级临时报告（不入库） |

升级只删除 `distribution-layout.yml` 中登记的旧 Harness 文件，不保存旧框架备份；项目自己的同目录内容保持不动。

## 版本戳

`.harness/state/harness-version.txt` 记录最近一次 `install` 同步的框架 SHA + 时间，便于追溯。

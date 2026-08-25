# .harness/state/

CI/CD 运行时状态目录（原 `state/`，已于 v1.6 迁移至此）。

| 文件 | 写入者 | 说明 |
|------|--------|------|
| `test-env.lock` | `scripts/lock.py` | Test 环境互斥锁（SPRINT.md §发布编排） |
| `promotion-log.yml` | `scripts/promote.py` | 镜像与分支提升历史 |
| `harness-version.txt` | `install.sh` | 上次框架同步戳（ref / timestamp / branch） |
| `acceptance/*.yml` | `scripts/acceptance_record.py` | L3 走查审批记录 |
| `sprints/*.json` | `harness sprint activate/amend` | Sprint 结构激活证据，不是第二份 Sprint 台账 |
| `tasks/*.json` | `sprint-gate` / `task-review` | 当前任务 attempt 指针与门禁证据 |
| `dependency-sessions/*.json` | `harness dependency` | 跨工程 Library Sprint、candidate、消费验证和采用状态 |
| `library-packages/*.json` | `harness library-package` | Build Once wheel 的版本、源码提交与 SHA-256 证据 |

任务 Plan、Review 和历史 attempt 位于 `.harness/runs/<sprint>/<task>/attempt-N/`，属于可丢弃过程数据并由 `.harness/.gitignore` 排除；长期知识只能进入 `docs/` 中对应的产品、设计、技术、测试或验收目录。

约束：
- 文件由脚本管理；人手修改前先 `lock check`。
- `test-env.lock` 在 promote / release CD 之间互斥。
- TTL 默认 7200 秒，过期自动释放。

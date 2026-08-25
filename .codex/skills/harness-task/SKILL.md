---
name: harness-task
description: 在活动 Sprint 中通过强制 Preflight、唯一执行协议、结构化 Review 和回退路线执行单个任务。
---

# Harness 任务

1. 读取 `AGENTS.md`、活动 Sprint 计划，以及 `.harness/rules/task-rules.yml` 中当前任务类型的规则块。通用 `acceptance` 只与当前 `project.type` 的 `acceptance_by_project_type` 合并，不得套用其他工程类型条件。
2. 执行 `uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-path> --task-id <task-id> --strict`。输入、模式、前置任务或 Preflight 任一失败都必须停止。
3. 运行 `harness task-context` 获取 attempt 路径和唯一协议。`execution_protocol=agent` 才调用 `harness-plan` → `harness-exec`；计划只写入 `.harness/runs/`。`action` 只调用 `harness task-action`；`orchestrator` 由前台按规则 steps 执行。禁止同时寻找第二入口。
4. `review_protocol=agent-full` 时独立调用 `harness-review`；`artifact-only` 时前台只根据 Action/Gate 的确定性证据生成 Review JSON，不派生 Review Agent。两者都必须写入 `task-context.review_report`，并执行 `task-review` 和 Review Gate。
5. 最终 PASS 必须 `scope=full` 并覆盖全部稳定验收 ID；`focused` 只能返回 FAIL。
6. FAIL 时使用 `--new-attempt --increment-retry` 重新进入 Preflight，再按同一协议执行。默认最多重试 2 次；修复任务与父任务共享预算。重置必须附 `--reset-retry-reason`，旧 attempt 证据失效；L3 审批始终留在前台。

本 Skill 只定义通用执行协议。任务差异只存在于 task-rules；确定性检查和状态转换由 Python Runtime 承担。

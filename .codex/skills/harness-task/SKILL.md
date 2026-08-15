---
name: harness-task
description: 在活动的 Feature、Deploy、Hotfix、Control 或 Maintenance Sprint 中，通过强制 Preflight、Plan、Exec 和独立 Review 循环执行单个任务。
---

# Harness 任务

1. 读取 `AGENTS.md`、活动 Sprint 计划，以及 `.harness/rules/task-rules.yml` 中当前任务类型的规则块。通用 `acceptance` 只与当前 `project.stack` 的 `acceptance_by_stack` 合并，不得套用其他技术栈条件。
2. 执行 `uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-path> --task-id <task-id> --strict`。输入、模式、前置任务或 Preflight 任一失败都必须停止。
3. 调用 `harness-plan`，只传任务 ID/类型、Sprint 路径、声明输入、上游产物、规范、输出和验收条件。
4. 规则声明 `entry_action` 或 `execute.action` 时，通过 `uv run --project .harness/runtime harness task-action <task-type> --task-id <task-id> entry|execute --sprint <sprint-path> [--value key=value]` 执行；非确定性工作把已批准计划原样交给 `harness-exec`。
5. 使用原验收条件和真实产物独立调用 `harness-review`。保存报告后执行 `uv run --project .harness/runtime harness task-review <task-type> --task-id <task-id> <sprint-path> --report <path> --decision pass|fail --artifact <实际文件> [--artifact <文件> ...]`。只有登记 PASS 证据后才能执行 `uv run --project .harness/runtime harness sprint-gate <task-type> <sprint-path> --task-id <task-id> --phase review --strict`；最终 Gate 会运行 `artifact_action` 进行硬性输出检查。
6. FAIL 时使用 `--new-attempt --increment-retry` 重新进入 Preflight，再完整执行 Plan → Exec → Review。旧 attempt 证据失效；L3 审批始终留在前台。

本 Skill 只定义通用执行协议。任务差异只存在于 task-rules；确定性检查和状态转换由 Python Runtime 承担。

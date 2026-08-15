---
name: harness-garden
description: 通过 Automation Heartbeat 执行文档或代码治理，并提供互斥锁、去重、持久运行记录和受控修复策略。
---

# Harness 仓库治理

计划任务统一使用 `uv run --project .harness/runtime harness heartbeat run <job>` 作为入口。

1. 首次以 report-only 执行，并检查 `.harness/automation/latest/<job>.json`。
2. 使用 `uv run --project .harness/runtime harness heartbeat findings` 读取跨运行归一化 Finding。
3. Lint 失败是硬门禁；Garden Finding 只是待分诊输入。
4. 不得重复成功的 run key，也不得重复已有 `active_pr` 的 Finding。
5. 只有 `config/harness.yml` 中 `automation.autonomy.enabled: true`、`mode: safe-fix`，当前 job/severity 位于 allowlist、未超过 attempt 上限、且配置确定性的 `fix_commands.<job>` 时才允许修复；protected paths 始终不可修改。
6. 通过 `uv run --project .harness/runtime harness autofix <finding-fingerprint>` 修复；该入口负责 diff 保护、Secret 扫描、测试、重新扫描和 PR/triage 状态。

不得因为扫描建议就修改受保护路径或自动合并。

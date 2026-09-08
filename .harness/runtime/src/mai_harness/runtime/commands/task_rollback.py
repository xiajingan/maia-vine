#!/usr/bin/env python3
"""Roll a Sprint task back and append an auditable reason."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.sprint_context import table_rows

VALID_STATES = re.compile(
    r"^(done|completed|通过|完成|in-progress|进行中|pending|待开始|blocked|阻塞|failed|失败|spawned|已派发|rollback|✅|⏳|⬜|🔄)$",
    re.I,
)


def rollback(
    content: str, from_task: str, to_task: str, reason: str, *, timestamp: str | None = None
) -> tuple[str, int]:
    lines = content.split("\n")
    rolled = 0
    target = re.escape(to_task)
    key_value = re.compile(rf"^(\s*[-*#]+.*?\b{target}\b[^\n]*?(?:status|状态)\s*[:：]\s*)(\S+)", re.I)
    for index, line in enumerate(lines):
        if re.match(r"^\s*\|", line) and to_task in line and re.search(r"\|[^|]*\|\s*$", line):
            cells = line.split("|")
            current = cells[-2].strip()
            if current and VALID_STATES.match(current.split()[0]):
                cells[-2] = " rollback "
                lines[index] = "|".join(cells)
                rolled += 1
                continue
        if key_value.search(line):
            lines[index] = key_value.sub(r"\1rollback", line)
            rolled += 1
    output = "\n".join(lines)
    stamp = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
    entry = f"- [{stamp}] {from_task} → {to_task}: {reason}"
    header = "## 回退日志"
    if header in output:
        start = output.index(header)
        next_header = re.search(r"\n## ", output[start + len(header) :])
        position = start + len(header) + (next_header.start() if next_header else len(output[start + len(header) :]))
        output = output[:position].rstrip() + "\n" + entry + "\n" + output[position:].lstrip("\n")
    else:
        output = output.rstrip() + f"\n\n{header}\n\n{entry}\n"
    return output, rolled


def rollback_types(
    content: str,
    from_task: str,
    task_types: set[str],
    reason: str,
    *,
    timestamp: str | None = None,
) -> tuple[str, int]:
    """Roll back every affected task type with one auditable log entry."""
    lines = content.split("\n")
    rolled = 0
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("|")
            and any(cell.strip().casefold() in {"类型", "type"} for cell in line.strip().strip("|").split("|"))
            and any(cell.strip().casefold() in {"状态", "status"} for cell in line.strip().strip("|").split("|"))
        ),
        -1,
    )
    if header_index >= 0:
        headers = [cell.strip().casefold() for cell in lines[header_index].strip().strip("|").split("|")]
        type_index = next(index for index, value in enumerate(headers) if value in {"类型", "type"})
        status_index = next(index for index, value in enumerate(headers) if value in {"状态", "status"})
    else:
        type_index = status_index = -1
    for index in range(header_index + 2, len(lines)):
        line = lines[index]
        if header_index < 0 or not line.lstrip().startswith("|"):
            if header_index >= 0:
                break
            continue
        values = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(values) <= max(type_index, status_index) or values[type_index] not in task_types:
            continue
        current = values[status_index]
        if current and VALID_STATES.match(current.split()[0]):
            values[status_index] = "rollback"
            lines[index] = "| " + " | ".join(values) + " |"
            rolled += 1
    output = "\n".join(lines)
    stamp = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
    entry = f"- [{stamp}] {from_task} → {','.join(sorted(task_types))}: {reason}"
    header = "## 回退日志"
    if header in output:
        start = output.index(header)
        next_header = re.search(r"\n## ", output[start + len(header) :])
        position = start + len(header) + (next_header.start() if next_header else len(output[start + len(header) :]))
        output = output[:position].rstrip() + "\n" + entry + "\n" + output[position:].lstrip("\n")
    else:
        output = output.rstrip() + f"\n\n{header}\n\n{entry}\n"
    return output, rolled


def reopen_scope(
    content: str,
    stages: list[Any],
    from_task_id: str,
    responsible_scope: str,
    owner_types: set[str],
    reason: str,
    *,
    timestamp: str | None = None,
) -> tuple[str, list[str]]:
    """Rollback the responsible stage and every planned downstream task."""
    if not owner_types:
        raise ValueError(f"责任范围 {responsible_scope} 缺少配置的 owner_tasks")

    def stage_types(stage: Any) -> set[str]:
        raw = stage.get("tasks", []) if isinstance(stage, dict) else stage
        return {str(value) for value in raw} if isinstance(raw, list) else set()

    rows = table_rows(content)
    from_row = next((row for row in rows if row.get("id") == from_task_id), None)
    if from_row is None:
        raise ValueError(f"Review 来源任务未登记在 Sprint: {from_task_id}")
    from_type = from_row.get("类型") or from_row.get("type") or ""
    owner_stage = next((index for index, stage in enumerate(stages) if stage_types(stage) & owner_types), -1)
    current_stage = next((index for index, stage in enumerate(stages) if from_type in stage_types(stage)), -1)
    if owner_stage < 0:
        raise ValueError(f"当前 Sprint 类型没有 {responsible_scope} 责任阶段，无法建立回退路线")
    if current_stage < owner_stage:
        raise ValueError("Review 不得把范围冲突指向当前任务之后的责任阶段")
    downstream_types = {name for stage in stages[owner_stage:] for name in stage_types(stage)}
    affected = [
        row.get("id", "") for row in rows if (row.get("类型") or row.get("type")) in downstream_types and row.get("id")
    ]
    updated, count = rollback_types(
        content,
        from_task_id,
        downstream_types,
        f"scope-conflict({responsible_scope}): {reason}",
        timestamp=timestamp,
    )
    if count != len(affected):
        raise ValueError("Sprint 任务状态无法完整执行传递性回退")
    return updated, affected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("from_task")
    parser.add_argument("to_task")
    parser.add_argument("sprint_plan_file", type=Path)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if not args.sprint_plan_file.exists():
        parser.error(f"Sprint 计划文件不存在: {args.sprint_plan_file}")
    updated, count = rollback(
        args.sprint_plan_file.read_text(encoding="utf-8"),
        args.from_task,
        args.to_task,
        args.reason or f"{args.from_task} 不达标",
    )
    args.sprint_plan_file.write_text(updated, encoding="utf-8")
    print(
        f"{args.to_task} 的 {count} 处 status 已标记为 rollback"
        if count
        else f"未找到 {args.to_task} 的 status 行；已追加回退日志"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Roll a Sprint task back and append an auditable reason."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

VALID_STATES = re.compile(
    r"^(done|completed|通过|完成|in-progress|进行中|pending|待开始|blocked|阻塞|rollback|✅|⏳|⬜|🔄)$", re.I
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

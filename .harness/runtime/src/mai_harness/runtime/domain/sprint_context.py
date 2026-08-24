"""Pure parsing and policy helpers for Sprint context."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SPRINT_ID = re.compile(r"^sprint-\d+-[a-z0-9][a-z0-9-]*$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def header_field(content: str, field: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*`?([^`\n]+?)`?\s*$", content)
    return match.group(1).strip() if match else ""


def sprint_header(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    return {field: header_field(content, field) for field in ("sprint_type", "base_ref", "base_sha", "branch")}


def table_rows(content: str) -> list[dict[str, str]]:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        headers = [cell.strip().lower() for cell in line.strip().strip("|").split("|")]
        if "id" not in headers or not ({"类型", "type"} & set(headers)):
            continue
        if index + 1 >= len(lines):
            return []
        rows: list[dict[str, str]] = []
        for value in lines[index + 2 :]:
            if not value.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells, strict=True)))
        return rows
    return []


def sprint_structure_digest(path: Path) -> str:
    """Hash immutable lifecycle fields and task structure, excluding prose/status."""
    content = path.read_text(encoding="utf-8")
    rows = []
    for row in table_rows(content):
        rows.append({key: value for key, value in row.items() if key not in {"status", "状态"}})
    value = {"header": sprint_header(path), "tasks": rows}
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def sprint_policy(rules: dict[str, Any], sprint_type: str) -> dict[str, str]:
    policy = (rules.get("sprint_type_policies") or {}).get(sprint_type)
    if not isinstance(policy, dict):
        raise ValueError(f"Sprint 类型缺少生命周期策略: {sprint_type}")
    return {key: str(value) for key, value in policy.items()}


def branch_name(sprint_id: str, policy: dict[str, str]) -> str:
    prefix = policy.get("branch_prefix", "")
    suffix = sprint_id.removeprefix("sprint-")
    return f"{prefix}{suffix}" if prefix else ""

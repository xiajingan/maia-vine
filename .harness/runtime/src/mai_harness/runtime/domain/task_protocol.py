"""Derive the single execution and review protocol for a task rule."""

from __future__ import annotations

from typing import Any


def execution_protocol(task: dict[str, Any]) -> str:
    declared = task.get("execution_protocol")
    if declared:
        return str(declared)
    if task.get("executor") == "orchestrator":
        return "orchestrator"
    if task.get("entry_action") or (task.get("execute") or {}).get("action"):
        return "action"
    return "agent"


def review_protocol(task: dict[str, Any]) -> str:
    declared = task.get("review_protocol")
    if declared:
        return str(declared)
    return "artifact-only" if execution_protocol(task) == "orchestrator" else "agent-full"

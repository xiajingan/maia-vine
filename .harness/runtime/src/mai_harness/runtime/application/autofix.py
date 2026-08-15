"""Policy-bounded safe-fix state machine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mai_harness.runtime.domain.pipeline_plan import can_autofix


def run_safe_fix(
    *,
    policy: dict[str, Any],
    finding: dict[str, Any],
    fix: Callable[[dict[str, Any]], None],
    changed_files: Callable[[], list[str]],
    validate: Callable[[list[str]], dict[str, Any]],
    create_pr: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    if finding.get("active_pr"):
        return {"status": "skipped", "reason": "active-pr", "finding": finding}
    if not can_autofix(policy, {**finding, "touches": []}):
        return {"status": "triage", "reason": "policy", "finding": finding}
    attempted = {**finding, "attempts": finding.get("attempts", 0) + 1, "status": "fixing"}
    try:
        fix(attempted)
        touches = changed_files()
        if not touches:
            return {"status": "triage", "reason": "empty-diff", "finding": attempted}
        if not can_autofix(policy, {**finding, "touches": touches}):
            return {"status": "triage", "reason": "protected-diff", "touches": touches, "finding": attempted}
        validation = validate(touches)
        if not validation.get("ok"):
            return {
                "status": "triage",
                "reason": "validation-failed",
                "touches": touches,
                "validation": validation,
                "finding": attempted,
            }
        pr = create_pr({"finding": attempted, "touches": touches, "validation": validation})
        return {
            "status": "pr-open",
            "touches": touches,
            "validation": validation,
            "pr": pr,
            "finding": {**attempted, "status": "in-review", "active_pr": pr},
        }
    except Exception as exc:
        return {"status": "triage", "reason": "execution-failed", "error": str(exc), "finding": attempted}

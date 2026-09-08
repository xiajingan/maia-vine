#!/usr/bin/env python3
"""Bind a successful Test promotion to its exact task attempt and delivery inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.integration_contract import (
    build_delivery_identity,
    healthy_deploy_states,
)
from mai_harness.runtime.application.promotion_preflight import validate_promotion_preflight
from mai_harness.runtime.application.task_evidence import current_attempt_state_path
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def _plan(root: Path, sprint_id: str) -> Path | None:
    return next(
        (
            path
            for directory in ("active", "completed")
            if (path := root / "docs/exec-plans" / directory / f"{Path(sprint_id).name}.md").is_file()
        ),
        None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    sprint = _plan(root, args.sprint)
    if sprint is None:
        parser.error(f"Sprint 计划不存在: {args.sprint}")
    attempt_path = current_attempt_state_path(root, sprint, args.task_id)
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"❌ promote-test attempt 无法读取: {attempt_path}: {exc}")
        return 1
    if (
        attempt.get("schema_version") != 3
        or attempt.get("sprint") != sprint.stem
        or attempt.get("task_id") != args.task_id
        or attempt.get("task_type") != "promote-test"
        or attempt.get("status") != "ready"
        or not attempt.get("ready_at")
        or attempt.get("review") is not None
    ):
        print("❌ promote-test 回执只能由当前 ready attempt 在部署后、Review 前生成")
        return 1
    build_identity, errors = build_delivery_identity(root, sprint.stem)
    task_binding = {
        "sprint": sprint.stem,
        "task_id": args.task_id,
        "attempt_run_id": attempt["run_id"],
        "attempt": attempt["attempt"],
    }
    preflight_receipt, preflight_errors = validate_promotion_preflight(root, "test", task_binding)
    deploy_states, deploy_errors = healthy_deploy_states(
        root,
        build_identity,
        task_binding,
        preflight_receipt,
        not_before=str(attempt["ready_at"]),
    )
    errors.extend(preflight_errors)
    errors.extend(deploy_errors)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        return 1
    receipt = {
        "schema_version": 1,
        "sprint": sprint.stem,
        "task_id": args.task_id,
        "attempt": {
            "path": attempt_path.relative_to(root).as_posix(),
            "run_id": attempt["run_id"],
            "number": attempt["attempt"],
            "ready_at": attempt["ready_at"],
        },
        "build_identity": build_identity,
        "preflight_receipt": preflight_receipt,
        "deploy_states": deploy_states,
    }
    filename = f"promote-test/{sprint.stem}/{args.task_id}/{attempt['run_id']}.json"
    store = StateStore(root / ".harness/state")
    existing = store.read_json(filename, None)
    if existing is not None and existing != receipt:
        print("❌ 当前 promote-test attempt 的不可变回执已存在且内容不同")
        return 1
    target = store.write_json(filename, receipt)
    print(f"✅ promote-test 回执已绑定当前 task attempt: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

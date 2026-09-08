#!/usr/bin/env python3
"""Execute one registered integration producer and persist immutable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.integration_contract import (
    delivery_identity,
    integration_contract,
    protected_worktree_digest,
    validate_integration_contract,
)
from mai_harness.runtime.application.task_evidence import require_planned_attempt
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config, resolve_command
from mai_harness.runtime.infrastructure.utils import load_yaml, write_yaml

PRODUCER_TIMEOUT_SECONDS = 1700


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except (PermissionError, OSError):
        return True
    return True


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
    rules = load_yaml(HarnessPaths.detect(project=root).rules / "task-rules.yml")
    task = (rules.get("tasks") or {}).get("integration") if isinstance(rules, dict) else None
    if not isinstance(task, dict):
        parser.error("task-rules.yml 缺少 integration 任务")
    config = load_harness_config()
    contract = integration_contract(sprint, args.task_id, task)
    if errors := validate_integration_contract(contract, task, config):
        for error in errors:
            print(f"❌ {error}")
        return 1
    store = StateStore(root / ".harness/state")
    rules_path = HarnessPaths.detect(project=root).rules / "task-rules.yml"
    try:
        attempt = require_planned_attempt(root, sprint, rules_path, args.task_id, "integration", task)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    run_id = str(attempt["run_id"])
    filename = f"integration-exec/{sprint.stem}/{args.task_id}/{run_id}.json"
    if store.path(filename).exists():
        print("❌ 当前 integration attempt 已执行过 producer；禁止覆盖或自动重跑")
        return 1
    previous_paths = list(
        (root / ".harness/state/integration-exec" / sprint.stem / args.task_id).glob("*.json")
    )
    if contract["write_intent"] == "controlled-write":
        recovery_of = str(contract["recovery_of"])
        if previous_paths:
            previous_path = max(previous_paths, key=lambda path: path.stat().st_mtime_ns)
            try:
                previous = json.loads(previous_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                print(f"❌ 上一次 integration producer 状态损坏，拒绝恢复: {exc}")
                return 1
            previous_status = str(previous.get("status", ""))
            if previous_status == "running":
                if _pid_alive(previous.get("pid")):
                    print("❌ 上一次 integration producer 仍在运行，拒绝并发执行")
                    return 1
                previous_status = "abandoned"
            previous_run_id = str((previous.get("attempt") or {}).get("run_id", ""))
            if previous_status == "passed":
                print("❌ 上一次 producer 已成功；禁止对同一任务自动重复写入，请规划新的 integration 任务")
                return 1
            if (
                recovery_of != previous_run_id
                or previous_status not in {"failed", "abandoned"}
                or (previous.get("contract") or {}).get("checkpoint") != contract["checkpoint"]
            ):
                print("❌ 写入恢复必须通过 recovery_of 绑定上一次失败 run，并复用同一 checkpoint")
                return 1
        elif recovery_of.lower() not in {"", "-", "none", "n/a"}:
            print("❌ 首次写入 integration 不得声明 recovery_of")
            return 1
    identity_before, identity_errors = delivery_identity(root, sprint.stem)
    if identity_errors:
        for error in identity_errors:
            print(f"❌ {error}")
        return 1
    output = str((task.get("outputs") or {}).get("path", "")).strip().rstrip("/")
    excluded = [Path(output) / args.task_id] if output else []
    protected_before = protected_worktree_digest(root, excluded)
    producer = str(contract["producer"])
    command_name = producer.removeprefix("command:") if producer.startswith("command:") else ""
    command = resolve_command(config.get("commands", {}).get(command_name, [])) if command_name else []
    started_at = datetime.now(UTC).isoformat()
    environment = {
        **os.environ,
        "HARNESS_INTEGRATION_SPRINT": sprint.stem,
        "HARNESS_INTEGRATION_TASK_ID": args.task_id,
        "HARNESS_INTEGRATION_ENVIRONMENT": str(contract["environment"]),
        "HARNESS_INTEGRATION_CHECKPOINT": str(contract["checkpoint"]),
        "HARNESS_INTEGRATION_FAILURE_STRATEGY": str(contract["failure_strategy"]),
    }
    state = {
        "schema_version": 1,
        "sprint": sprint.stem,
        "task_id": args.task_id,
        "attempt": {
            "path": f".harness/state/tasks/task-{sprint.stem}--{args.task_id}.json",
            "run_id": run_id,
            "number": attempt["attempt"],
        },
        "contract": {key: value for key, value in contract.items() if key != "facets_explicit"},
        "producer": producer,
        "command_sha256": hashlib.sha256(json.dumps(command, ensure_ascii=False).encode()).hexdigest(),
        "delivery_identity": identity_before,
        "protected_worktree_before": protected_before,
        "started_at": started_at,
        "execution_id": run_id,
        "pid": os.getpid(),
        "status": "running",
        "success": False,
    }
    target = store.write_json(filename, state)
    outcome = None
    identity_after: dict = {}
    after_errors: list[str] = []
    protected_after = "unavailable"
    success = False
    execution_error = ""
    try:
        outcome = (
            execute(
                CommandSpec.argv_command(
                    command,
                    cwd=root,
                    env=environment,
                    timeout_seconds=PRODUCER_TIMEOUT_SECONDS,
                    terminate_process_group=True,
                )
            )
            if command
            else None
        )
        identity_after, after_errors = delivery_identity(root, sprint.stem)
        protected_after = protected_worktree_digest(root, excluded)
        success = (
            (outcome is None or outcome.ok)
            and not after_errors
            and identity_before == identity_after
            and protected_before == protected_after
        )
    except Exception as exc:  # Always leave a terminal audit record for recovery.
        execution_error = f"{type(exc).__name__}: {exc}"
    finally:
        state.update(
            {
                "returncode": outcome.returncode if outcome else (0 if not execution_error else 1),
                "failure_kind": outcome.failure_kind if outcome else ("internal" if execution_error else None),
                "execution_error": execution_error,
                "stdout_sha256": hashlib.sha256((outcome.stdout if outcome else "").encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256((outcome.stderr if outcome else "").encode()).hexdigest(),
                "delivery_identity_unchanged": identity_before == identity_after and not after_errors,
                "delivery_identity_errors": after_errors,
                "protected_worktree_after": protected_after,
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "passed" if success else "failed",
                "success": success,
            }
        )
        target = store.write_json(filename, state)
    if success:
        receipt = root / "docs/test-reports/integration" / args.task_id / "receipt.yml"
        write_yaml(
            receipt,
            {
                "schema_version": 1,
                "task_id": args.task_id,
                "environment": contract["environment"],
                "contract": state["contract"],
                "delivery_identity": identity_before,
                "execution_state": {
                    "path": target.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                },
                "operations": [
                    {
                        "action": producer if command else "delivery-identity-preflight",
                        "target": contract["environment"],
                        "scope": "TODO",
                        "result": "pending",
                    }
                ],
                "checkpoint": contract["checkpoint"],
                "failure": {
                    "occurred": False,
                    "strategy": contract["failure_strategy"],
                    "details": "",
                },
                "evidence": [],
                "health": {"result": "pending", "evidence_sha256": []},
                "result": "pending",
            },
        )
    print(json.dumps({"ok": success, "evidence": str(target)}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Attempt-scoped evidence for deterministic task transitions and independent review."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_json(value: Any) -> str:
    return _digest_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _safe_name(sprint: str, task_type: str) -> str:
    safe = "--".join((sprint, task_type))
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in safe):
        raise ValueError("Sprint 与任务类型只能包含字母、数字、点、下划线和连字符")
    return f"task-{safe}.json"


def _git_sha(root: Path) -> str:
    result = execute(CommandSpec.argv_command(("git", "rev-parse", "HEAD"), cwd=root))
    return result.stdout.strip() if result.ok else "unversioned"


def _sprint_structure_digest(path: Path) -> str:
    """Hash a Sprint plan while ignoring mutable task status cells."""
    lines = path.read_text(encoding="utf-8").splitlines()
    status_index = -1
    normalized = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        if cells and any(cell.lower() in {"status", "状态"} for cell in cells):
            status_index = next(index for index, cell in enumerate(cells) if cell.lower() in {"status", "状态"})
        elif status_index >= 0 and cells and status_index < len(cells):
            if all(set(cell) <= {"-", ":"} for cell in cells):
                pass
            else:
                cells[status_index] = "<status>"
                line = "| " + " | ".join(cells) + " |"
        elif status_index >= 0 and not cells:
            status_index = -1
        normalized.append(line)
    return _digest_bytes(("\n".join(normalized) + "\n").encode())


def _context(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, str]:
    return {
        "sprint_sha256": _digest_bytes(sprint_path.read_bytes()),
        "sprint_structure_sha256": _sprint_structure_digest(sprint_path),
        "rules_sha256": _digest_bytes(rules_path.read_bytes()),
        "task_sha256": _digest_json(task),
        "git_sha": _git_sha(root),
        "task_id": task_id,
        "task_type": task_type,
    }


def _state(root: Path, sprint_path: Path, task_id: str) -> tuple[StateStore, str]:
    return StateStore(root / ".harness/state/tasks"), _safe_name(sprint_path.stem, task_id)


def _declared_output_paths(task: dict[str, Any]) -> tuple[list[Path], Path | None]:
    """Return mechanically verifiable output roots/indexes from task rules."""
    outputs = task.get("outputs") or {}
    raw = outputs.get("path") if isinstance(outputs, dict) else None
    roots: list[Path] = []
    if isinstance(raw, str) and not any(token in raw for token in ("<", "项目根目录", "URL")):
        for value in raw.split(" 或 "):
            candidate = value.strip().rstrip("/")
            if candidate and not Path(candidate).is_absolute() and ".." not in Path(candidate).parts:
                roots.append(Path(candidate))
    index = outputs.get("index") if isinstance(outputs, dict) else None
    index_path = (
        Path(index)
        if isinstance(index, str)
        and "<" not in index
        and not Path(index).is_absolute()
        and ".." not in Path(index).parts
        else None
    )
    return roots, index_path


def effective_acceptance(task: dict[str, Any]) -> list[Any]:
    """Resolve common plus current-stack acceptance from the executable config."""
    stack = load_harness_config()["project"]["stack"]
    return [*(task.get("acceptance") or []), *((task.get("acceptance_by_stack") or {}).get(stack) or [])]


def ensure_attempt(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    *,
    new_attempt: bool = False,
) -> dict[str, Any]:
    store, name = _state(root, sprint_path, task_id)
    expected = _context(root, sprint_path, rules_path, task_id, task_type, task)

    def update(current: Any) -> dict[str, Any]:
        current = current if isinstance(current, dict) else {}
        # Starting Preflight revokes any previous ready authorization first.
        # A repeated failed Preflight may reuse the same pending record, but a
        # previously executable attempt can never survive a new Preflight run.
        if new_attempt or current.get("context") != expected or current.get("status") != "pending":
            return {
                "schema_version": 2,
                "run_id": uuid.uuid4().hex,
                "attempt": int(current.get("attempt", 0)) + 1,
                "sprint": sprint_path.stem,
                "task_id": task_id,
                "task_type": task_type,
                "status": "pending",
                "context": expected,
                "phases": {},
                "review": None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        return current

    return store.update_json(name, update, {})


def load_current_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    store, name = _state(root, sprint_path, task_id)
    state = store.read_json(name, {})
    if not isinstance(state, dict) or state.get("schema_version") != 2:
        raise ValueError("任务执行轮次不存在；先运行 preflight gate")
    if state.get("context") != _context(root, sprint_path, rules_path, task_id, task_type, task):
        raise ValueError("任务输入已变化；必须重新运行 preflight gate 创建新轮次")
    return state


def require_ready_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    """Reject action/review side effects until the exact task attempt passed Preflight."""
    state = load_current_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    if state.get("status") != "ready":
        raise ValueError("任务 Preflight 尚未通过，拒绝执行 Action")
    return state


def activate_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> Path:
    state = load_current_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    store, name = _state(root, sprint_path, task_id)

    def update(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("run_id") != state["run_id"]:
            raise ValueError("任务执行轮次已变化，拒绝激活")
        current["status"] = "ready"
        current["ready_at"] = datetime.now(UTC).isoformat()
        return current

    store.update_json(name, update, {})
    return store.path(name)


def record_phase(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    phase: str,
    action: str,
    values: dict[str, str],
    returncode: int,
    artifacts: list[Path] | None = None,
) -> Path:
    state = require_ready_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    store, name = _state(root, sprint_path, task_id)

    def update(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("run_id") != state["run_id"]:
            raise ValueError("任务执行轮次已变化，拒绝写入旧轮次证据")
        artifact_records = []
        for path in artifacts or []:
            resolved = path.resolve()
            if resolved.is_file() and resolved.is_relative_to(root.resolve()):
                artifact_records.append(
                    {
                        "path": resolved.relative_to(root.resolve()).as_posix(),
                        "sha256": _digest_bytes(resolved.read_bytes()),
                    }
                )
        current.setdefault("phases", {})[phase] = {
            "action": action,
            "values_sha256": _digest_json(values),
            "returncode": returncode,
            "artifacts": artifact_records,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        return current

    store.update_json(name, update, {})
    return store.path(name)


def validate_failed_action_evidence(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    expected_action: str,
) -> list[str]:
    """Validate immutable artifacts recorded by a failed execute Action."""
    store, name = _state(root, sprint_path, task_id)
    try:
        state = store.read_json(name, {})
    except (OSError, ValueError) as exc:
        return [f"上游失败 Action 证据损坏: {task_id} ({exc})"]
    if not isinstance(state, dict) or state.get("schema_version") != 2 or state.get("status") != "ready":
        return [f"上游失败 Action 证据不存在: {task_id} ({task_type})"]
    context = state.get("context", {}) if isinstance(state, dict) else {}
    if not isinstance(context, dict):
        return [f"上游失败 Action context 格式非法: {task_id} ({task_type})"]
    expected_context = _context(root, sprint_path, rules_path, task_id, task_type, task)
    context_fields = ("task_id", "task_type", "task_sha256", "rules_sha256", "git_sha", "sprint_structure_sha256")
    if any(context.get(field) != expected_context.get(field) for field in context_fields):
        return [f"上游失败 Action 证据不存在: {task_id} ({task_type})"]
    phases = state.get("phases") or {}
    phase = (phases.get("execute") or {}) if isinstance(phases, dict) else {}
    if not isinstance(phase, dict) or phase.get("action") != expected_action:
        return [f"上游失败 Action 类型不匹配: {phase.get('action', '缺失')} != {expected_action}"]
    if type(phase.get("returncode")) is not int or phase["returncode"] == 0:
        return [f"上游 Action 未记录失败退出码: {task_id} ({task_type})"]
    artifacts = phase.get("artifacts") or []
    if not isinstance(artifacts, list) or not artifacts or not all(isinstance(item, dict) for item in artifacts):
        return [f"上游失败 Action 未记录产物: {task_id} ({task_type})"]
    errors = []
    for artifact in artifacts:
        relative = Path(str(artifact.get("path", "")))
        path = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(root.resolve())
            or not path.is_file()
            or _digest_bytes(path.read_bytes()) != artifact.get("sha256")
        ):
            errors.append(f"上游失败 Action 产物缺失或已变化: {path}")
    return errors


def record_review(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    report_path: Path,
    decision: str,
    artifacts: list[Path],
) -> Path:
    state = require_ready_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    report = report_path.resolve()
    if not report.is_file() or not report.is_relative_to(root.resolve()):
        raise ValueError("Review report 必须是工程内已存在文件")
    if decision == "pass" and not artifacts:
        raise ValueError("Review 至少需要一个实际产物 --artifact")
    artifact_records = []
    artifact_paths: list[Path] = []
    for value in artifacts:
        artifact = value.resolve()
        if not artifact.is_file() or not artifact.is_relative_to(root.resolve()):
            raise ValueError(f"Review artifact 必须是工程内文件: {value}")
        artifact_records.append(
            {"path": artifact.relative_to(root.resolve()).as_posix(), "sha256": _digest_bytes(artifact.read_bytes())}
        )
        artifact_paths.append(artifact.relative_to(root.resolve()))
    execute_artifacts = {
        item.get("path")
        for item in ((state.get("phases") or {}).get("execute") or {}).get("artifacts", [])
        if item.get("path")
    }
    execute_action = (task.get("execute") or {}).get("action", "")
    if decision == "pass" and execute_action.startswith("control.") and task.get("outputs"):
        if not execute_artifacts:
            raise ValueError("Control execute Action 未记录本轮产物")
        if not execute_artifacts.intersection(path.as_posix() for path in artifact_paths):
            raise ValueError("Review artifact 必须包含本轮 execute Action 产生的文件")
        phase_records = {item["path"]: item["sha256"] for item in state["phases"]["execute"]["artifacts"]}
        for record in artifact_records:
            if record["path"] in phase_records and record["sha256"] != phase_records[record["path"]]:
                raise ValueError("Review artifact 内容与 execute Action 完成时不一致")
    declared_roots, declared_index = _declared_output_paths(task)
    if (
        decision == "pass"
        and declared_roots
        and not any(
            artifact == declared or artifact.is_relative_to(declared)
            for artifact in artifact_paths
            for declared in declared_roots
        )
    ):
        raise ValueError(f"Review 产物未覆盖任务声明输出目录: {', '.join(map(str, declared_roots))}")
    if decision == "pass" and declared_index and declared_index not in artifact_paths:
        raise ValueError(f"Review 必须绑定任务声明索引: {declared_index}")
    store, name = _state(root, sprint_path, task_id)

    def update(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("run_id") != state["run_id"]:
            raise ValueError("任务执行轮次已变化，拒绝写入 Review 证据")
        current["review"] = {
            "decision": decision,
            "report": report.relative_to(root.resolve()).as_posix(),
            "report_sha256": _digest_bytes(report.read_bytes()),
            "acceptance_sha256": _digest_json(effective_acceptance(task)),
            "artifacts": artifact_records,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        return current

    store.update_json(name, update, {})
    return store.path(name)


def validate_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> list[str]:
    try:
        state = load_current_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    if state.get("status") != "ready":
        errors.append("任务 Preflight 尚未通过")
    phases = state.get("phases") or {}
    for phase, action in (
        ("entry", task.get("entry_action")),
        ("execute", (task.get("execute") or {}).get("action")),
    ):
        evidence = phases.get(phase) or {}
        if action and (evidence.get("returncode") != 0 or evidence.get("action") != action):
            errors.append(f"缺少当前轮次成功的 {phase} action 证据: {action}")
        for artifact in evidence.get("artifacts") or []:
            path = root / artifact.get("path", "")
            if not path.is_file() or _digest_bytes(path.read_bytes()) != artifact.get("sha256"):
                errors.append(f"{phase} Action 产物缺失或已变化: {artifact.get('path', '')}")
    review = state.get("review") or {}
    report = root / str(review.get("report", ""))
    if review.get("decision") != "pass":
        errors.append("缺少当前轮次 harness-review PASS 证据")
    elif not report.is_file() or _digest_bytes(report.read_bytes()) != review.get("report_sha256"):
        errors.append("Review report 缺失或已变化")
    elif review.get("acceptance_sha256") != _digest_json(effective_acceptance(task)):
        errors.append("Review 验收条件与当前规则不一致")
    for artifact in review.get("artifacts") or []:
        path = root / str(artifact.get("path", ""))
        if not path.is_file() or _digest_bytes(path.read_bytes()) != artifact.get("sha256"):
            errors.append(f"Review 产物缺失或已变化: {artifact.get('path', '')}")
    if not review.get("artifacts"):
        errors.append("Review 未绑定实际产物")
    return errors

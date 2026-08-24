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
        "acceptance_sha256": _digest_json(acceptance_records(task, task_type)),
    }


def _state(root: Path, sprint_path: Path, task_id: str) -> tuple[StateStore, str]:
    return StateStore(root / ".harness/state/tasks"), _safe_name(sprint_path.stem, task_id)


def _run_dir(root: Path, sprint_path: Path, task_id: str, attempt: int) -> Path:
    safe_task = _safe_name("run", task_id).removeprefix("task-run--").removesuffix(".json")
    return root / ".harness/runs" / sprint_path.stem / safe_task / f"attempt-{attempt}"


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


def acceptance_records(task: dict[str, Any], task_type: str = "task") -> list[dict[str, str]]:
    """Resolve stable criterion IDs from explicit records or legacy string rules."""
    stack = load_harness_config()["project"]["stack"]
    raw = [*(task.get("acceptance") or []), *((task.get("acceptance_by_stack") or {}).get(stack) or [])]
    records: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("text"), str):
            records.append({"id": item["id"], "text": item["text"]})
            continue
        if not isinstance(item, str) or not item:
            raise ValueError(f"{task_type} acceptance 必须是非空字符串或 id/text 对象")
        criterion = item if all(char.islower() or char.isdigit() or char in ".-" for char in item) else ""
        if not criterion:
            criterion = f"{task_type}.criterion-{_digest_bytes(item.encode())[:12]}"
        records.append({"id": criterion, "text": item})
    if len({item["id"] for item in records}) != len(records):
        raise ValueError(f"{task_type} acceptance ID 重复")
    return records


def effective_acceptance(task: dict[str, Any]) -> list[dict[str, str]]:
    """Compatibility wrapper used by evidence digests."""
    return acceptance_records(task)


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
            if current.get("attempt"):
                archive = _run_dir(root, sprint_path, task_id, int(current["attempt"]))
                StateStore(archive).write_json("attempt.json", current)
            attempt = int(current.get("attempt", 0)) + 1
            run_dir = _run_dir(root, sprint_path, task_id, attempt)
            run_dir.mkdir(parents=True, exist_ok=True)
            return {
                "schema_version": 3,
                "run_id": uuid.uuid4().hex,
                "attempt": attempt,
                "sprint": sprint_path.stem,
                "task_id": task_id,
                "task_type": task_type,
                "status": "pending",
                "context": expected,
                "phases": {},
                "review": None,
                "run_dir": run_dir.relative_to(root).as_posix(),
                "plan": (run_dir / "plan.md").relative_to(root).as_posix(),
                "review_report": (run_dir / "review.json").relative_to(root).as_posix(),
                "created_at": datetime.now(UTC).isoformat(),
            }
        return current

    return store.update_json(name, update, {})


def load_current_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    store, name = _state(root, sprint_path, task_id)
    state = store.read_json(name, {})
    if not isinstance(state, dict) or state.get("schema_version") != 3:
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
    if not isinstance(state, dict) or state.get("schema_version") != 3 or state.get("status") != "ready":
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
    expected_report = root / state["review_report"]
    if report != expected_report.resolve():
        raise ValueError(f"Review report 必须写入当前 attempt: {expected_report}")
    try:
        review_document = json.loads(report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"Review report 必须是合法 JSON: {exc}") from exc
    _validate_review_document(review_document, decision, acceptance_records(task, task_type))
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
            "acceptance_sha256": _digest_json(acceptance_records(task, task_type)),
            "scope": review_document["scope"],
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
    elif review.get("acceptance_sha256") != _digest_json(acceptance_records(task, task_type)):
        errors.append("Review 验收条件与当前规则不一致")
    for artifact in review.get("artifacts") or []:
        path = root / str(artifact.get("path", ""))
        if not path.is_file() or _digest_bytes(path.read_bytes()) != artifact.get("sha256"):
            errors.append(f"Review 产物缺失或已变化: {artifact.get('path', '')}")
    if not review.get("artifacts"):
        errors.append("Review 未绑定实际产物")
    return errors


def _validate_review_document(document: Any, decision: str, acceptance: list[dict[str, str]]) -> None:
    if not isinstance(document, dict):
        raise ValueError("Review report 顶层必须是对象")
    if document.get("decision") != decision:
        raise ValueError("Review report decision 与命令参数不一致")
    scope = document.get("scope")
    if scope not in {"focused", "full"}:
        raise ValueError("Review report scope 必须是 focused 或 full")
    if decision == "pass" and scope != "full":
        raise ValueError("focused Review 不能产生最终 PASS")
    findings = document.get("findings")
    criteria = document.get("criteria")
    if not isinstance(findings, list) or not isinstance(criteria, list):
        raise ValueError("Review report 必须包含 findings 与 criteria 数组")
    acceptance_ids = {item["id"] for item in acceptance}
    seen: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict) or item.get("acceptance_id") not in acceptance_ids:
            raise ValueError("Review criteria 引用了未知 acceptance_id")
        if item.get("status") not in {"pass", "fail", "not-reviewed"}:
            raise ValueError("Review criteria.status 非法")
        if item["acceptance_id"] in seen:
            raise ValueError("Review criteria.acceptance_id 重复")
        if item["status"] != "not-reviewed" and not str(item.get("evidence", "")).strip():
            raise ValueError("Review criteria 通过或失败时必须提供 evidence")
        seen.add(item["acceptance_id"])
    if decision == "pass" and (seen != acceptance_ids or any(item.get("status") != "pass" for item in criteria)):
        raise ValueError("PASS Review 必须完整覆盖且通过全部验收条件")
    finding_ids: set[str] = set()
    for item in findings:
        required = {"finding_id", "acceptance_id", "severity", "evidence", "impact", "remediation", "blocking"}
        if not isinstance(item, dict) or not required <= set(item):
            raise ValueError("Review finding 缺少结构化必填字段")
        if item["acceptance_id"] not in acceptance_ids:
            raise ValueError("Review finding 引用了未知 acceptance_id")
        if item["severity"] not in {"critical", "major", "minor", "suggestion"}:
            raise ValueError("Review finding severity 非法")
        if type(item["blocking"]) is not bool:
            raise ValueError("Review finding blocking 必须是布尔值")
        if any(not str(item[field]).strip() for field in ("finding_id", "evidence", "impact", "remediation")):
            raise ValueError("Review finding 文本字段必须非空")
        if item["finding_id"] in finding_ids:
            raise ValueError("Review finding_id 重复")
        finding_ids.add(item["finding_id"])
    if decision == "pass" and any(item.get("blocking") for item in findings):
        raise ValueError("PASS Review 不得包含 blocking finding")
    if decision == "fail" and not any(item.get("blocking") for item in findings):
        raise ValueError("FAIL Review 必须包含至少一个 blocking finding")

"""Attempt-scoped evidence for deterministic task transitions and independent review."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.sprint_context import table_rows
from mai_harness.runtime.domain.task_protocol import execution_protocol, review_protocol
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


AGENT_INVOCATION = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,191}$")
AGENT_ROLES = {"plan", "exec", "review"}
PR_TASK_TYPES = {"pr", "library-pr"}
PR_GIT_POLICY_VERSION = 1


def agent_invocation_id(run_id: str, role: str) -> str:
    if role not in AGENT_ROLES:
        raise ValueError(f"非法 Agent role: {role}")
    return f"{run_id}:{role}"


def required_agent_roles(task: dict[str, Any]) -> set[str]:
    roles = {"plan", "exec"} if execution_protocol(task) == "agent" else set()
    if review_protocol(task) == "agent-full":
        roles.add("review")
    return roles


def _git_sha(root: Path) -> str:
    result = execute(CommandSpec.argv_command(("git", "rev-parse", "HEAD"), cwd=root))
    return result.stdout.strip() if result.ok else "unversioned"


def _git_branch(root: Path) -> str:
    result = execute(CommandSpec.argv_command(("git", "symbolic-ref", "--quiet", "--short", "HEAD"), cwd=root))
    return result.stdout.strip() if result.ok else "detached"


def _is_direct_child(root: Path, parent: str, child: str) -> bool:
    if parent in {"", "unversioned"} or child in {"", "unversioned"}:
        return False
    result = execute(CommandSpec.argv_command(("git", "rev-list", "--parents", "-n", "1", child), cwd=root))
    fields = result.stdout.strip().split() if result.ok else []
    return len(fields) == 2 and fields == [child, parent]


def _sprint_structure_digest(path: Path) -> str:
    """Hash a Sprint plan while ignoring mutable task status cells."""
    return _sprint_structure_digest_bytes(path.read_bytes())


def _sprint_structure_digest_bytes(value: bytes) -> str:
    lines = value.decode("utf-8").splitlines()
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


def _pre_archive_close_plan(sprint_path: Path, task_id: str, task_type: str) -> bytes | None:
    """Reverse the only mutations allowed while a close task archives its plan."""

    if task_type not in {"sprint-close", "library-close"} or sprint_path.parent.name != "completed":
        return None
    active_path = sprint_path.parent.parent / "active" / sprint_path.name
    if active_path.exists():
        return None
    lines = sprint_path.read_bytes().decode("utf-8").splitlines(keepends=True)
    header_count = 0
    for index, line in enumerate(lines):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        if body not in {"- **状态**：completed", "- **状态**: completed"}:
            continue
        lines[index] = body.removesuffix("completed") + "active" + ending
        header_count += 1
    if header_count != 1:
        return None
    status_index = type_index = id_index = -1
    changed = 0
    for index, line in enumerate(lines):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[: -len(ending)] if ending else line
        raw_cells = body[1:-1].split("|") if body.startswith("|") and body.endswith("|") else []
        cells = [cell.strip() for cell in raw_cells]
        if cells and any(cell.lower() in {"status", "状态"} for cell in cells):
            status_index = next(position for position, cell in enumerate(cells) if cell.lower() in {"status", "状态"})
            type_index = next((position for position, cell in enumerate(cells) if cell.lower() in {"type", "类型"}), -1)
            id_index = next((position for position, cell in enumerate(cells) if cell.lower() == "id"), -1)
            continue
        if min(status_index, type_index, id_index) < 0 or len(cells) <= max(status_index, type_index, id_index):
            continue
        if cells[id_index] != task_id:
            continue
        if cells[type_index] != task_type or raw_cells[status_index] != " done ":
            return None
        raw_cells[status_index] = " pending "
        lines[index] = "|" + "|".join(raw_cells) + "|" + ending
        changed += 1
    if changed != 1:
        return None
    return "".join(lines).encode()


def _matches_controlled_close_archive(
    sprint_path: Path,
    task_id: str,
    task_type: str,
    current: dict[str, str],
    expected: dict[str, str],
) -> bool:
    source = _pre_archive_close_plan(sprint_path, task_id, task_type)
    if source is None:
        return False
    archived_expected = {
        **expected,
        "sprint_sha256": _digest_bytes(source),
        "sprint_structure_sha256": _sprint_structure_digest_bytes(source),
    }
    return current == archived_expected


def _context(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    facets = task_facets(sprint_path, task_id, task)
    context = {
        "sprint_sha256": _digest_bytes(sprint_path.read_bytes()),
        "sprint_structure_sha256": _sprint_structure_digest(sprint_path),
        "rules_sha256": _digest_bytes(rules_path.read_bytes()),
        "task_sha256": _digest_json(task),
        "git_sha": _git_sha(root),
        "task_id": task_id,
        "task_type": task_type,
        "facets": facets,
        "acceptance_sha256": _digest_json(acceptance_records(task, task_type, facets)),
    }
    if task_type in PR_TASK_TYPES:
        context["git_branch"] = _git_branch(root)
    return context


def _matches_registered_pr_head(state: dict[str, Any], expected: dict[str, Any], task_type: str) -> bool:
    if task_type not in PR_TASK_TYPES:
        return False
    current = state.get("context")
    lineage = state.get("git_lineage")
    if not isinstance(current, dict) or not isinstance(lineage, dict):
        return False
    static_fields = set(expected) - {"git_sha"}
    if any(current.get(field) != expected.get(field) for field in static_fields):
        return False
    return (
        lineage.get("policy_version") == PR_GIT_POLICY_VERSION
        and lineage.get("branch") == expected.get("git_branch")
        and lineage.get("base_sha") == current.get("git_sha")
        and lineage.get("head_sha") == expected.get("git_sha")
    )


def _state(root: Path, sprint_path: Path, task_id: str) -> tuple[StateStore, str]:
    return StateStore(root / ".harness/state/tasks"), _safe_name(sprint_path.stem, task_id)


def _run_dir(root: Path, sprint_path: Path, task_id: str, attempt: int) -> Path:
    safe_task = _safe_name("run", task_id).removeprefix("task-run--").removesuffix(".json")
    return root / ".harness/runs" / sprint_path.stem / safe_task / f"attempt-{attempt}"


def task_facets(sprint_path: Path, task_id: str, task: dict[str, Any]) -> list[str]:
    """Resolve optional per-task review facets from the Sprint row or task defaults."""
    project_type = load_harness_config()["project"]["type"]
    supported = task.get("facets") or []
    defaults = (task.get("default_facets_by_project_type") or {}).get(project_type, task.get("default_facets") or [])
    rows = table_rows(sprint_path.read_text(encoding="utf-8")) if sprint_path.is_file() else []
    row = next((item for item in rows if item.get("id") == task_id), {})
    raw = row.get("facets") or row.get("任务属性") or row.get("能力面") or ""
    selected = [value.strip() for value in raw.replace("，", ",").split(",") if value.strip()] or list(defaults)
    if supported and not set(selected) <= set(supported):
        unknown = ", ".join(sorted(set(selected) - set(supported)))
        raise ValueError(f"任务 {task_id} 声明了不支持的 facets: {unknown}")
    return selected


def _finding_ledger(root: Path, sprint_path: Path, task_id: str) -> tuple[StateStore, str]:
    return StateStore(root / ".harness/state/tasks/findings"), _safe_name(sprint_path.stem, task_id)


def finding_ledger(root: Path, sprint_path: Path, task_id: str) -> dict[str, Any]:
    store, name = _finding_ledger(root, sprint_path, task_id)
    value = store.read_json(name, {"schema_version": 1, "findings": {}})
    return value if isinstance(value, dict) else {"schema_version": 1, "findings": {}}


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


def acceptance_records(
    task: dict[str, Any], task_type: str = "task", facets: list[str] | None = None
) -> list[dict[str, str]]:
    """Resolve stable criterion IDs from explicit records or legacy string rules."""
    project_type = load_harness_config()["project"]["type"]
    raw = [
        *(task.get("acceptance") or []),
        *((task.get("acceptance_by_project_type") or {}).get(project_type) or []),
    ]
    for facet in facets or []:
        raw.extend((task.get("acceptance_by_facet") or {}).get(facet) or [])
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
        if (
            new_attempt
            or current.get("context") != expected
            or current.get("status") != "pending"
            or current.get("agent_policy_version") != 1
        ):
            if current.get("attempt"):
                archive = _run_dir(root, sprint_path, task_id, int(current["attempt"]))
                StateStore(archive).write_json("attempt.json", current)
            attempt = int(current.get("attempt", 0)) + 1
            run_dir = _run_dir(root, sprint_path, task_id, attempt)
            run_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "schema_version": 3,
                "agent_policy_version": 1,
                "run_id": uuid.uuid4().hex,
                "attempt": attempt,
                "sprint": sprint_path.stem,
                "task_id": task_id,
                "task_type": task_type,
                "status": "pending",
                "context": expected,
                "phases": {},
                "agent_invocations": {},
                "review": None,
                "run_dir": run_dir.relative_to(root).as_posix(),
                "plan": (run_dir / "plan.md").relative_to(root).as_posix(),
                "review_report": (run_dir / "review.json").relative_to(root).as_posix(),
                "created_at": datetime.now(UTC).isoformat(),
            }
            if task_type in PR_TASK_TYPES:
                state["git_lineage"] = {
                    "policy_version": PR_GIT_POLICY_VERSION,
                    "branch": expected["git_branch"],
                    "base_sha": expected["git_sha"],
                    "head_sha": expected["git_sha"],
                }
            return state
        return current

    return store.update_json(name, update, {})


def load_current_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    store, name = _state(root, sprint_path, task_id)
    state = store.read_json(name, {})
    if not isinstance(state, dict) or state.get("schema_version") != 3:
        raise ValueError("任务执行轮次不存在；先运行 preflight gate")
    expected = _context(root, sprint_path, rules_path, task_id, task_type, task)
    current = state.get("context")
    pr_identity_mismatch = task_type in PR_TASK_TYPES and not _matches_registered_pr_head(state, expected, task_type)
    ordinary_identity_mismatch = (
        task_type not in PR_TASK_TYPES
        and current != expected
        and not (
            isinstance(current, dict)
            and _matches_controlled_close_archive(sprint_path, task_id, task_type, current, expected)
        )
    )
    if pr_identity_mismatch or ordinary_identity_mismatch:
        raise ValueError("任务输入已变化；必须重新运行 preflight gate 创建新轮次")
    return state


def advance_pr_head(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    *,
    invocation_id: str,
) -> dict[str, Any]:
    """Authorize a PR task's new linear HEAD using its bound Exec invocation."""
    if task_type not in PR_TASK_TYPES:
        raise ValueError("Git HEAD 推进只允许用于 pr/library-pr 任务")
    store, name = _state(root, sprint_path, task_id)
    expected = _context(root, sprint_path, rules_path, task_id, task_type, task)

    def update(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("schema_version") != 3:
            raise ValueError("任务执行轮次不存在；先运行 preflight gate")
        if current.get("status") != "ready":
            raise ValueError("任务 Preflight 尚未通过，拒绝登记 Git HEAD")
        context = current.get("context")
        lineage = current.get("git_lineage")
        if not isinstance(context, dict) or not isinstance(lineage, dict):
            raise ValueError("当前 PR attempt 早于 Git 身份绑定策略；必须重新运行 Preflight")
        static_fields = set(expected) - {"git_sha"}
        if any(context.get(field) != expected.get(field) for field in static_fields):
            raise ValueError("任务输入或 Git 分支已变化；拒绝登记 Git HEAD")
        if (
            lineage.get("policy_version") != PR_GIT_POLICY_VERSION
            or lineage.get("branch") != expected.get("git_branch")
            or lineage.get("base_sha") != context.get("git_sha")
        ):
            raise ValueError("PR Git 身份绑定记录无效；必须重新运行 Preflight")
        binding = (current.get("agent_invocations") or {}).get("exec")
        if not isinstance(binding, dict) or binding.get("invocation_id") != invocation_id:
            raise ValueError("只有当前 attempt 已绑定的 Exec invocation 可以登记 Git HEAD")
        previous = str(lineage.get("head_sha", ""))
        observed = str(expected.get("git_sha", ""))
        if previous == observed:
            raise ValueError("Git HEAD 未产生新提交")
        if not _is_direct_child(root, previous, observed):
            raise ValueError("Git HEAD 不是当前 attempt 已登记 HEAD 的单一直接子提交；拒绝跳跃登记、merge 或历史改写")
        lineage["head_sha"] = observed
        lineage["advanced_by"] = invocation_id
        lineage["advanced_at"] = datetime.now(UTC).isoformat()
        return current

    return store.update_json(name, update, {})


def require_ready_attempt(
    root: Path, sprint_path: Path, rules_path: Path, task_id: str, task_type: str, task: dict[str, Any]
) -> dict[str, Any]:
    """Reject action/review side effects until the exact task attempt passed Preflight."""
    state = load_current_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    if state.get("status") != "ready":
        raise ValueError("任务 Preflight 尚未通过，拒绝执行 Action")
    if state.get("agent_policy_version") != 1 or not isinstance(state.get("agent_invocations"), dict):
        raise ValueError("当前轮次早于 Agent 隔离策略；必须重新运行 Preflight 创建新 attempt")
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


def bind_agent_invocation(
    root: Path,
    sprint_path: Path,
    rules_path: Path,
    task_id: str,
    task_type: str,
    task: dict[str, Any],
    *,
    role: str,
    invocation_id: str,
    runtime: str,
) -> dict[str, Any]:
    """Bind one declared fresh Agent invocation to the current task attempt."""
    state = require_ready_attempt(root, sprint_path, rules_path, task_id, task_type, task)
    if role not in required_agent_roles(task):
        raise ValueError(f"当前任务协议不允许 {role} Agent")
    expected = agent_invocation_id(state["run_id"], role)
    if invocation_id != expected or not AGENT_INVOCATION.fullmatch(invocation_id):
        raise ValueError(f"Agent invocation 与当前 attempt/role 不匹配: {role}")
    if not runtime or len(runtime) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", runtime):
        raise ValueError("Agent runtime 标识非法")
    binding = {
        "invocation_id": invocation_id,
        "role": role,
        "runtime": runtime,
        "run_id": state["run_id"],
        "task_id": task_id,
        "task_type": task_type,
        "attempt": state["attempt"],
    }
    store, name = _state(root, sprint_path, task_id)

    def update_state(current: Any) -> dict[str, Any]:
        if not isinstance(current, dict) or current.get("run_id") != state["run_id"]:
            raise ValueError("任务执行轮次已变化，拒绝绑定 Agent")
        bindings = current.setdefault("agent_invocations", {})
        existing = bindings.get(role)
        if existing is not None:
            raise ValueError("Agent invocation 已使用；每个实例只能绑定一次")
        bindings[role] = {
            **binding,
            "bound_at": datetime.now(UTC).isoformat(),
        }
        return current

    store.update_json(name, update_state, {})
    return binding


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
    diagnostic: dict[str, Any] | None = None,
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
        phase_record = {
            "action": action,
            "values_sha256": _digest_json(values),
            "returncode": returncode,
            "artifacts": artifact_records,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        if returncode != 0 and diagnostic:
            phase_record["diagnostic"] = diagnostic
        current.setdefault("phases", {})[phase] = phase_record
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
    facets = list((state.get("context") or {}).get("facets") or [])
    acceptance = acceptance_records(task, task_type, facets)
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
    _validate_review_document(review_document, decision, acceptance)
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
            "acceptance_sha256": _digest_json(acceptance),
            "scope": review_document["scope"],
            "artifacts": artifact_records,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        return current

    store.update_json(name, update, {})
    _record_finding_ledger(root, sprint_path, task_id, state, review_document)
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
    if state.get("agent_policy_version") != 1:
        errors.append("当前轮次早于 Agent 隔离策略；必须重新运行 Preflight 创建新 attempt")
    else:
        bindings = state.get("agent_invocations") or {}
        for role in sorted(required_agent_roles(task)):
            binding = bindings.get(role) if isinstance(bindings, dict) else None
            expected = agent_invocation_id(str(state.get("run_id", "")), role)
            if not isinstance(binding, dict) or binding.get("invocation_id") != expected:
                errors.append(f"缺少当前轮次全新 {role} Agent invocation 证据")
        invocation_ids = (
            [
                item.get("invocation_id")
                for item in bindings.values()
                if isinstance(item, dict) and item.get("invocation_id")
            ]
            if isinstance(bindings, dict)
            else []
        )
        if len(invocation_ids) != len(set(invocation_ids)):
            errors.append("Plan/Exec/Review Agent invocation 必须相互独立")
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
    facets = list((state.get("context") or {}).get("facets") or [])
    report = root / str(review.get("report", ""))
    if review.get("decision") != "pass":
        errors.append("缺少当前轮次 harness-review PASS 证据")
    elif not report.is_file() or _digest_bytes(report.read_bytes()) != review.get("report_sha256"):
        errors.append("Review report 缺失或已变化")
    elif review.get("acceptance_sha256") != _digest_json(acceptance_records(task, task_type, facets)):
        errors.append("Review 验收条件与当前规则不一致")
    for artifact in review.get("artifacts") or []:
        path = root / str(artifact.get("path", ""))
        if not path.is_file() or _digest_bytes(path.read_bytes()) != artifact.get("sha256"):
            errors.append(f"Review 产物缺失或已变化: {artifact.get('path', '')}")
    if not review.get("artifacts"):
        errors.append("Review 未绑定实际产物")
    return errors


def _finding_fingerprint(item: dict[str, Any]) -> str:
    identity = {
        "acceptance_id": item["acceptance_id"],
        "finding_key": item["finding_key"],
        "finding_type": item["finding_type"],
        "violated_invariant": item["violated_invariant"],
    }
    return _digest_json(identity)


def _record_finding_ledger(
    root: Path,
    sprint_path: Path,
    task_id: str,
    state: dict[str, Any],
    review: dict[str, Any],
) -> None:
    store, name = _finding_ledger(root, sprint_path, task_id)
    now = datetime.now(UTC).isoformat()

    def update(current: Any) -> dict[str, Any]:
        current = current if isinstance(current, dict) else {}
        entries = current.get("findings") if isinstance(current.get("findings"), dict) else {}
        seen: set[str] = set()
        for item in review["findings"]:
            fingerprint = _finding_fingerprint(item)
            seen.add(fingerprint)
            previous = entries.get(fingerprint) if isinstance(entries.get(fingerprint), dict) else {}
            status = "reopened" if previous.get("status") == "closed" else "open"
            entries[fingerprint] = {
                **previous,
                "fingerprint": fingerprint,
                "finding_key": item["finding_key"],
                "acceptance_id": item["acceptance_id"],
                "finding_type": item["finding_type"],
                "quality_attributes": item["quality_attributes"],
                "violated_invariant": item["violated_invariant"],
                "status": status,
                "first_seen_attempt": previous.get("first_seen_attempt", state["attempt"]),
                "last_seen_attempt": state["attempt"],
                "last_finding_id": item["finding_id"],
                "last_seen_at": now,
            }
        if review["scope"] == "full":
            for fingerprint, item in entries.items():
                if fingerprint not in seen and item.get("status") in {"open", "reopened"}:
                    item["status"] = "closed"
                    item["closed_attempt"] = state["attempt"]
                    item["closed_at"] = now
        return {"schema_version": 1, "findings": entries, "updated_at": now}

    store.update_json(name, update, {"schema_version": 1, "findings": {}})


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
        if item.get("status") not in {"pass", "fail", "incomplete", "not-reviewed"}:
            raise ValueError("Review criteria.status 非法")
        if item["acceptance_id"] in seen:
            raise ValueError("Review criteria.acceptance_id 重复")
        if item["status"] != "not-reviewed" and not str(item.get("evidence", "")).strip():
            raise ValueError("Review criteria 通过或失败时必须提供 evidence")
        seen.add(item["acceptance_id"])
    if decision == "pass" and (seen != acceptance_ids or any(item.get("status") != "pass" for item in criteria)):
        raise ValueError("PASS Review 必须完整覆盖且通过全部验收条件")
    finding_ids: set[str] = set()
    finding_keys: set[str] = set()
    for item in findings:
        required = {
            "finding_id",
            "finding_key",
            "finding_type",
            "acceptance_id",
            "severity",
            "evidence",
            "impact",
            "remediation",
            "blocking",
            "violated_invariant",
            "scenario",
            "observable_failure",
            "quality_attributes",
            "scope_relation",
        }
        if not isinstance(item, dict) or not required <= set(item):
            raise ValueError("Review finding 缺少结构化必填字段")
        if item["acceptance_id"] not in acceptance_ids:
            raise ValueError("Review finding 引用了未知 acceptance_id")
        if item["severity"] not in {"critical", "major", "minor", "suggestion"}:
            raise ValueError("Review finding severity 非法")
        if item["finding_type"] not in {
            "defect",
            "regression",
            "evidence_gap",
            "environment_blocker",
            "scope_conflict",
        }:
            raise ValueError("Review finding_type 非法")
        if item["scope_relation"] not in {"in_scope", "regression", "pre_existing", "out_of_scope"}:
            raise ValueError("Review scope_relation 非法")
        attributes = item["quality_attributes"]
        allowed_attributes = {
            "correctness",
            "reliability",
            "availability",
            "scalability",
            "concurrency",
            "security",
            "maintainability",
            "simplicity",
            "performance",
            "operability",
            "test_assurance",
        }
        if not isinstance(attributes, list) or not attributes or not set(attributes) <= allowed_attributes:
            raise ValueError("Review quality_attributes 非法或为空")
        if type(item["blocking"]) is not bool:
            raise ValueError("Review finding blocking 必须是布尔值")
        if any(
            not str(item[field]).strip()
            for field in (
                "finding_id",
                "finding_key",
                "evidence",
                "impact",
                "remediation",
                "violated_invariant",
                "scenario",
                "observable_failure",
            )
        ):
            raise ValueError("Review finding 文本字段必须非空")
        if item["blocking"] and (
            item["finding_type"] not in {"defect", "regression"}
            or item["scope_relation"] not in {"in_scope", "regression"}
        ):
            raise ValueError("只有范围内的 defect/regression 可以作为 blocking finding")
        if item["finding_id"] in finding_ids:
            raise ValueError("Review finding_id 重复")
        if item["finding_key"] in finding_keys:
            raise ValueError("Review finding_key 重复")
        finding_ids.add(item["finding_id"])
        finding_keys.add(item["finding_key"])
    if decision == "pass" and any(item.get("blocking") for item in findings):
        raise ValueError("PASS Review 不得包含 blocking finding")
    if decision == "fail" and not any(item.get("blocking") for item in findings):
        raise ValueError("FAIL Review 必须包含至少一个 blocking finding")
    if decision == "incomplete" and not (
        any(item.get("status") in {"incomplete", "not-reviewed"} for item in criteria)
        or any(
            item.get("finding_type") in {"evidence_gap", "environment_blocker", "scope_conflict"} for item in findings
        )
    ):
        raise ValueError("INCOMPLETE Review 必须声明证据、环境或范围缺口")

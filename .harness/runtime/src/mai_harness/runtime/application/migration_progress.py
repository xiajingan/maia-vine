"""Checkpointed migration progress with an immutable candidate identity."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import load_yaml, write_yaml

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
POLICY_KEYS = {
    "business_upgrade_rollback",
    "progress_model",
    "candidate_on_failure",
    "resume_from",
    "data_rollback",
    "failure_phases",
    "rollback_authority",
}
REQUIRED_POLICY = {
    "business_upgrade_rollback": "forbidden",
    "progress_model": "checkpoint",
    "candidate_on_failure": "preserve",
    "resume_from": "last-committed-checkpoint",
    "data_rollback": "forbidden",
    "failure_phases": ["migration", "quality-check", "service-start", "release"],
    "rollback_authority": "explicit-human-release-rollback",
}


def policy_errors(policy: Any, path: str = "migration_execution_policy") -> list[str]:
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        return [f"{path} 必须且只能声明 {sorted(POLICY_KEYS)}"]
    errors = [
        f"{path}.{key} 必须为 {expected!r}"
        for key, expected in REQUIRED_POLICY.items()
        if key != "failure_phases"
        if policy.get(key) != expected
    ]
    phases = policy.get("failure_phases")
    if (
        not isinstance(phases, list)
        or not phases
        or len(phases) != len(set(phases))
        or not all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in phases)
        or not set(REQUIRED_POLICY["failure_phases"]) <= set(phases)
    ):
        errors.append(f"{path}.failure_phases 必须是无重复稳定 ID，并至少包含 {REQUIRED_POLICY['failure_phases']!r}")
    return errors


def _now(value: str | None = None) -> str:
    return value or datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_identifier(value: str, field: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} 必须匹配 {IDENTIFIER.pattern}")
    return value


def _locked(operation):
    """Serialize every migration state transition across processes."""

    @wraps(operation)
    def invoke(root: Path, migration_id: str, *args, **kwargs):
        _ensure_identifier(migration_id, "migration_id")
        store = StateStore(root.resolve() / ".harness/state/migrations")
        with store.lock(f"{migration_id}.progress", timeout_seconds=30):
            return operation(root, migration_id, *args, **kwargs)

    return invoke


def progress_path(root: Path, migration_id: str) -> Path:
    return root.resolve() / ".harness/state/migrations" / f"{_ensure_identifier(migration_id, 'migration_id')}.yml"


def load_progress(root: Path, migration_id: str) -> dict[str, Any]:
    path = progress_path(root, migration_id)
    if not path.is_file():
        raise ValueError(f"迁移进度不存在: {migration_id}；先执行 begin")
    state = load_yaml(path)
    if not isinstance(state, dict) or state.get("migration_id") != migration_id:
        raise ValueError(f"迁移进度损坏: {path}")
    checkpoints = state.get("committed_checkpoints")
    checkpoint_ids = [item.get("id") for item in checkpoints] if isinstance(checkpoints, list) else []
    expected_resume = checkpoint_ids[-1] if checkpoint_ids else "start"
    valid_checkpoints = (
        isinstance(checkpoints, list)
        and all(
            isinstance(item, dict)
            and IDENTIFIER.fullmatch(str(item.get("id", "")))
            and item.get("sequence") == index
            and isinstance(item.get("summary"), str)
            and isinstance(item.get("committed_at"), str)
            for index, item in enumerate(checkpoints, 1)
        )
        and len(checkpoint_ids) == len(set(checkpoint_ids))
    )
    if (
        state.get("version") != 2
        or not isinstance(state.get("candidate_ref"), str)
        or not state["candidate_ref"].strip()
        or state.get("status") not in {"active", "failed", "completed"}
        or not valid_checkpoints
        or state.get("resume_from") != expected_resume
        or not isinstance(state.get("failures"), list)
        or (state.get("status") == "failed" and not state["failures"])
        or not IDENTIFIER.fullmatch(str(state.get("workflow_kind", "")))
        or not isinstance(state.get("required_checkpoints"), list)
        or len(state.get("required_checkpoints", [])) != len(set(state.get("required_checkpoints", [])))
        or not all(IDENTIFIER.fullmatch(str(item)) for item in state.get("required_checkpoints", []))
    ):
        raise ValueError(f"迁移进度不满足单调 checkpoint 契约: {path}")
    return state


@_locked
def begin(
    root: Path,
    migration_id: str,
    candidate_ref: str,
    *,
    workflow_kind: str = "migration",
    required_checkpoints: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if not candidate_ref.strip():
        raise ValueError("candidate_ref 不得为空")
    _ensure_identifier(workflow_kind, "workflow_kind")
    required = list(required_checkpoints or [])
    if len(required) != len(set(required)) or any(not IDENTIFIER.fullmatch(str(item)) for item in required):
        raise ValueError("required_checkpoints 必须是无重复稳定 ID 数组")
    path = progress_path(root, migration_id)
    if path.exists():
        state = load_progress(root, migration_id)
        if state.get("candidate_ref") != candidate_ref:
            raise ValueError("迁移已绑定其他 candidate；禁止替换候选数据或重新开始")
        if state.get("workflow_kind") != workflow_kind or state.get("required_checkpoints") != required:
            raise ValueError("进度 ID 已绑定其他 workflow/checkpoint graph")
        return state
    state = {
        "version": 2,
        "migration_id": migration_id,
        "candidate_ref": candidate_ref,
        "workflow_kind": workflow_kind,
        "required_checkpoints": required,
        "status": "active",
        "started_at": _now(now),
        "resume_from": "start",
        "committed_checkpoints": [],
        "failures": [],
    }
    write_yaml(path, state)
    return state


@_locked
def commit_checkpoint(
    root: Path,
    migration_id: str,
    checkpoint_id: str,
    summary: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    _ensure_identifier(checkpoint_id, "checkpoint_id")
    state = load_progress(root, migration_id)
    if state.get("status") != "active":
        raise ValueError("只有 active 迁移可提交 checkpoint；失败后先修复并执行 resume")
    existing = next((item for item in state.get("committed_checkpoints", []) if item.get("id") == checkpoint_id), None)
    if existing:
        if existing.get("summary") != summary:
            raise ValueError("同名 checkpoint 已提交且内容不同；提交进度不可改写")
        return state
    required = state.get("required_checkpoints", [])
    if checkpoint_id in required:
        prior = required[: required.index(checkpoint_id)]
        committed = {item.get("id") for item in state.get("committed_checkpoints", [])}
        if not set(prior) <= committed:
            raise ValueError(f"checkpoint {checkpoint_id} 缺少前置 required checkpoints: {prior}")
    checkpoint = {
        "id": checkpoint_id,
        "sequence": len(state.get("committed_checkpoints", [])) + 1,
        "summary": summary,
        "committed_at": _now(now),
    }
    state.setdefault("committed_checkpoints", []).append(checkpoint)
    state["resume_from"] = checkpoint_id
    write_yaml(progress_path(root, migration_id), state)
    return state


@_locked
def record_failure(
    root: Path,
    migration_id: str,
    phase: str,
    reason: str,
    *,
    now: str | None = None,
    allowed_phases: list[str] | None = None,
) -> dict[str, Any]:
    phases = allowed_phases or REQUIRED_POLICY["failure_phases"]
    if phase not in phases:
        raise ValueError(f"phase 必须是 {phases}")
    state = load_progress(root, migration_id)
    if state.get("status") == "failed":
        last = (state.get("failures") or [{}])[-1]
        if last.get("phase") == phase and last.get("reason") == reason:
            return state
        raise ValueError("迁移已处于 failed；修复问题后执行 resume，不得叠加尝试")
    if state.get("status") != "active":
        raise ValueError("只有 active 迁移可记录失败")
    candidate = state.get("candidate_ref")
    checkpoints = deepcopy(state.get("committed_checkpoints", []))
    state["status"] = "failed"
    state.setdefault("failures", []).append(
        {
            "phase": phase,
            "reason": reason,
            "failed_at": _now(now),
            "resume_from": state.get("resume_from", "start"),
        }
    )
    if state.get("candidate_ref") != candidate or state.get("committed_checkpoints") != checkpoints:
        raise RuntimeError("失败处理不得修改 candidate 或已提交 checkpoint")
    write_yaml(progress_path(root, migration_id), state)
    return state


@_locked
def resume(root: Path, migration_id: str, *, now: str | None = None) -> dict[str, Any]:
    state = load_progress(root, migration_id)
    if state.get("status") != "failed":
        raise ValueError("只有 failed 迁移可 resume")
    state["status"] = "active"
    state["resumed_at"] = _now(now)
    write_yaml(progress_path(root, migration_id), state)
    return state


@_locked
def complete(root: Path, migration_id: str, *, now: str | None = None) -> dict[str, Any]:
    state = load_progress(root, migration_id)
    if state.get("status") != "active":
        raise ValueError("只有 active 迁移可完成")
    if not state.get("committed_checkpoints"):
        raise ValueError("至少提交一个 checkpoint 后才能完成迁移")
    committed = {item.get("id") for item in state.get("committed_checkpoints", [])}
    missing = [item for item in state.get("required_checkpoints", []) if item not in committed]
    if missing:
        raise ValueError(f"workflow={state['workflow_kind']} 缺少 required checkpoints: {missing}")
    state["status"] = "completed"
    state["completed_at"] = _now(now)
    write_yaml(progress_path(root, migration_id), state)
    return state

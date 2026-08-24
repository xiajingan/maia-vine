"""Validate Sprint plans against repository and ephemeral lifecycle state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.sprint_context import (
    SPRINT_ID,
    branch_name,
    sprint_header,
    sprint_policy,
    sprint_structure_digest,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def _git(root: Path, *args: str) -> tuple[bool, str]:
    outcome = execute(CommandSpec.argv_command(("git", *args), cwd=root))
    return outcome.ok, outcome.stdout.strip()


def linked_worktree(root: Path) -> bool:
    git_ok, git_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    common_ok, common_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_ok or not common_ok:
        raise ValueError("当前目录不是可验证的 Git 工作区")
    return Path(git_dir).resolve() != Path(common_dir).resolve()


def validate_sprint_context(
    root: Path,
    sprint_path: Path,
    rules: dict[str, Any],
    *,
    allow_completed: bool = False,
) -> list[str]:
    """Validate the active plan against the real branch and worktree state."""
    errors: list[str] = []
    if not SPRINT_ID.fullmatch(sprint_path.stem):
        return [f"Sprint ID 非法: {sprint_path.stem}"]
    allowed_paths = [root / "docs/exec-plans/active" / sprint_path.name]
    if allow_completed:
        allowed_paths.append(root / "docs/exec-plans/completed" / sprint_path.name)
    if sprint_path.resolve() not in {path.resolve() for path in allowed_paths}:
        location = "active/ 或 completed/" if allow_completed else "active/"
        errors.append(f"Sprint 计划必须位于当前工作区 docs/exec-plans/{location}: {sprint_path}")
    try:
        header = sprint_header(sprint_path)
        policy = sprint_policy(rules, header["sprint_type"])
        is_linked = linked_worktree(root)
    except (OSError, UnicodeError, ValueError) as exc:
        return [str(exc)]
    worktree_policy = policy.get("worktree")
    if worktree_policy == "required" and not is_linked:
        errors.append("当前 Sprint 必须在独立 linked worktree 中执行")
    if worktree_policy == "forbidden" and is_linked:
        errors.append("当前 Sprint 类型禁止使用独立 linked worktree")
    expected_branch = branch_name(sprint_path.stem, policy)
    branch_ok, current_branch = _git(root, "branch", "--show-current")
    if not branch_ok or not current_branch:
        errors.append("无法确认当前 Git 分支")
    elif expected_branch and current_branch != expected_branch:
        errors.append(f"Sprint 分支不匹配: expected={expected_branch}, actual={current_branch}")
    if expected_branch and header.get("branch") != expected_branch:
        errors.append(f"Sprint 计划 branch 字段不匹配: {header.get('branch') or '缺失'}")
    base_ref, base_sha = header.get("base_ref", ""), header.get("base_sha", "")
    if not base_ref or not re.fullmatch(r"refs/remotes/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+", base_ref):
        errors.append("Sprint 计划缺少合法的远端 base_ref")
    if not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        errors.append("Sprint 计划缺少合法的 base_sha")
    elif not _git(root, "cat-file", "-e", f"{base_sha}^{{commit}}")[0]:
        errors.append(f"Sprint base_sha 不存在: {base_sha}")
    elif not _git(root, "merge-base", "--is-ancestor", base_sha, "HEAD")[0]:
        errors.append(f"Sprint HEAD 未包含登记基线: {base_sha}")
    return errors


def validate_sprint_activation(root: Path, sprint_path: Path) -> list[str]:
    store = StateStore(root / ".harness/state/sprints")
    state = store.read_json(f"{sprint_path.stem}.json", None)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return ["Sprint 尚未通过 harness sprint activate 激活"]
    if state.get("structure_sha256") != sprint_structure_digest(sprint_path):
        return ["Sprint 结构已变化；必须执行 harness sprint amend 并记录原因"]
    return []

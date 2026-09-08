"""Validate Sprint plans against repository and ephemeral lifecycle state."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mai_harness.runtime.application.requirements import (
    validate_partial_sprint_completion,
    validate_story_confirmations,
)
from mai_harness.runtime.domain.sprint_context import (
    SPRINT_ID,
    branch_name,
    duplicate_sprint_contract_fields,
    header_field,
    planning_contract_digest,
    sprint_header,
    sprint_outcome_path,
    sprint_planning_contract,
    sprint_policy,
    sprint_source_requirements_digest,
    sprint_structure_digest,
    sprint_uses_story_requirements,
    table_rows,
    validate_sprint_outcome_evidence,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def task_reopen_transfer_errors(
    root: Path,
    receipt: dict[str, Any],
    rules: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the immutable completed Sprint that resolved a transferred scope conflict."""
    required = {
        "transfer_sprint",
        "transfer_plan",
        "transfer_plan_sha256",
        "transfer_planning_contract_sha256",
        "transfer_outcome",
        "transfer_outcome_sha256",
        "transferred_at",
    }
    if receipt.get("status") != "transferred" or receipt.get("requires_transfer") is not False:
        return ["责任域转移 receipt 尚未标记为 transferred"]
    if required - set(receipt):
        return [f"责任域转移 receipt 缺少完成字段: {sorted(required - set(receipt))}"]
    target_id = receipt.get("transfer_sprint")
    if not isinstance(target_id, str) or not SPRINT_ID.fullmatch(target_id):
        return [f"责任域转移目标 Sprint ID 非法: {target_id}"]
    plan = root / "docs/exec-plans/completed" / f"{target_id}.md"
    outcome = sprint_outcome_path(root, target_id)
    if receipt.get("transfer_plan") != plan.relative_to(root).as_posix() or not plan.is_file():
        return [f"责任域转移必须绑定 completed Sprint 计划: {plan}"]
    if receipt.get("transfer_outcome") != outcome.relative_to(root).as_posix() or not outcome.is_file():
        return [f"责任域转移必须绑定 canonical Sprint outcome: {outcome}"]
    errors: list[str] = []
    if hashlib.sha256(plan.read_bytes()).hexdigest() != receipt.get("transfer_plan_sha256"):
        errors.append("责任域转移目标 Sprint 计划已变化")
    if planning_contract_digest(plan) != receipt.get("transfer_planning_contract_sha256"):
        errors.append("责任域转移目标 planning contract 已变化")
    if hashlib.sha256(outcome.read_bytes()).hexdigest() != receipt.get("transfer_outcome_sha256"):
        errors.append("责任域转移目标 Sprint outcome 已变化")
    if sprint_planning_contract(plan).get("planning_contract_version") != 3:
        errors.append("责任域转移目标必须使用 v3 planning contract")
    if sprint_header(plan).get("sprint_type") != receipt.get("transfer_to"):
        errors.append("责任域转移目标 Sprint 类型与配置不一致")
    expected_source = f"{receipt.get('sprint')}/{receipt.get('from_task_id')}/{receipt.get('from_run_id')}"
    if header_field(plan.read_text(encoding="utf-8"), "scope_transfer_from") != expected_source:
        errors.append(f"责任域转移目标计划必须声明 scope_transfer_from: {expected_source}")
    try:
        outcome_document = yaml.safe_load(outcome.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        errors.append(f"责任域转移目标 outcome 无法读取: {exc}")
        return errors
    producer = outcome_document.get("producer") if isinstance(outcome_document, dict) else None
    if not isinstance(producer, dict):
        errors.append("责任域转移目标 outcome 缺少可信 producer")
        return errors
    expected_kind = producer.get("kind")
    expected_task_id = producer.get("task_id") if expected_kind == "task-review" else None
    expected_task_type = producer.get("task_type") if expected_kind == "task-review" else None
    if rules is not None:
        target_contract = (rules.get("sprint_delivery_contracts") or {}).get(receipt.get("transfer_to")) or {}
        configured_kind = target_contract.get("outcome_producer")
        if expected_kind != configured_kind:
            errors.append(f"责任域转移目标 outcome producer 必须是 {configured_kind}，实际为 {expected_kind}")
        if configured_kind == "task-review":
            terminal_types = set(target_contract.get("terminal_tasks") or [])
            candidates = [
                row
                for row in table_rows(plan.read_text(encoding="utf-8"))
                if (row.get("类型") or row.get("type")) in terminal_types
                and row.get("id") == expected_task_id
                and (row.get("类型") or row.get("type")) == expected_task_type
            ]
            if len(candidates) != 1:
                errors.append("责任域转移目标 outcome producer 不是计划内唯一匹配的终态任务")
    errors.extend(
        validate_sprint_outcome_evidence(
            root,
            plan,
            expected_task_id,
            expected_task_type,
            expected_kind if isinstance(expected_kind, str) else None,
        )
    )
    return errors


def complete_task_reopen_transfer(
    root: Path,
    receipt: dict[str, Any],
    target_id: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Bind a transfer-required receipt to one completed and validated target Sprint."""
    plan = root / "docs/exec-plans/completed" / f"{target_id}.md"
    outcome = sprint_outcome_path(root, target_id)
    completed = {
        **receipt,
        "status": "transferred",
        "requires_transfer": False,
        "transfer_sprint": target_id,
        "transfer_plan": plan.relative_to(root).as_posix(),
        "transfer_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest() if plan.is_file() else "",
        "transfer_planning_contract_sha256": planning_contract_digest(plan) if plan.is_file() else "",
        "transfer_outcome": outcome.relative_to(root).as_posix(),
        "transfer_outcome_sha256": hashlib.sha256(outcome.read_bytes()).hexdigest() if outcome.is_file() else "",
        "transferred_at": datetime.now(UTC).isoformat(),
    }
    if errors := task_reopen_transfer_errors(root, completed, rules):
        raise ValueError("责任域转移尚未闭环:\n- " + "\n- ".join(errors))
    return completed


def pending_task_reopen_errors(root: Path, sprint_path: Path, state: dict[str, Any]) -> list[str]:
    """Keep scope rollbacks blocked until their required Sprint amendments close them."""
    directory = root / ".harness/state/task-reopens"
    if not directory.exists():
        return []
    errors: list[str] = []
    rows = table_rows(sprint_path.read_text(encoding="utf-8")) if sprint_path.is_file() else []
    rows_by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    contract = sprint_planning_contract(sprint_path) if sprint_path.is_file() else {}
    current_story_ids = {
        str(item.get("id"))
        for item in contract.get("source_stories") or []
        if isinstance(item, dict) and item.get("id")
    }
    for path in sorted(directory.glob(f"{sprint_path.stem}--*.json")):
        try:
            receipt = StateStore(directory).read_json(path.name, {})
        except (OSError, ValueError) as exc:
            errors.append(f"需求责任域回退 receipt 损坏: {path}: {exc}")
            continue
        required = {
            "schema_version",
            "sprint",
            "from_task_id",
            "from_run_id",
            "review_report",
            "review_report_sha256",
            "responsible_scope",
            "status",
            "requires_transfer",
            "requires_requirements_feedback",
            "requires_structure_amend",
            "recorded_at",
        }
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("sprint") != sprint_path.stem
            or required - set(receipt)
        ):
            errors.append(f"需求责任域回退 receipt 格式非法: {path}")
            continue
        if receipt.get("requires_transfer") is True:
            target = receipt.get("transfer_to")
            if not isinstance(target, str) or not target:
                errors.append(f"责任域转移 receipt 缺少目标 Sprint 类型: {path}")
            else:
                errors.append(
                    f"{receipt.get('responsible_scope')} 责任域必须转入新的 {target}；"
                    f"当前 Sprint 保持阻塞: {receipt.get('from_task_id')}"
                )
            continue
        if receipt.get("status") == "transferred":
            errors.extend(task_reopen_transfer_errors(root, receipt))
            continue
        if receipt.get("status") == "transfer-required":
            errors.append(f"责任域转移 receipt 状态与 requires_transfer 不一致: {path}")
            continue
        recorded_at = str(receipt.get("recorded_at", ""))
        amendments = [
            item
            for item in state.get("amendments", [])
            if isinstance(item, dict) and str(item.get("recorded_at", "")) > recorded_at
        ]
        if receipt.get("requires_requirements_feedback") is True:
            before = receipt.get("requirements_sha256_before")
            amended = any(item.get("requirements_changed") is True for item in amendments)
            selected_story_ids = {
                str(item.get("id"))
                for item in receipt.get("source_stories") or []
                if isinstance(item, dict) and item.get("id")
            }
            feedback_matches = False
            feedback_directory = root / ".harness/state/requirements/feedback"
            if feedback_directory.exists():
                for feedback_path in sorted(feedback_directory.glob(f"{sprint_path.stem}-*.json")):
                    try:
                        feedback = StateStore(feedback_directory).read_json(feedback_path.name, {})
                    except (OSError, ValueError) as exc:
                        errors.append(f"需求反馈 receipt 损坏: {feedback_path}: {exc}")
                        continue
                    if (
                        not isinstance(feedback, dict)
                        or feedback.get("schema_version") != 1
                        or feedback.get("sprint") != sprint_path.stem
                        or not isinstance(feedback.get("affected_story_ids"), list)
                    ):
                        errors.append(f"需求反馈 receipt 格式非法: {feedback_path}")
                        continue
                    affected = set(feedback["affected_story_ids"])
                    classification = feedback.get("classification")
                    story_match = (
                        bool(affected & selected_story_ids)
                        if classification == "requirement-change"
                        else bool(affected)
                        and not bool(affected & selected_story_ids)
                        and affected <= current_story_ids
                        if classification == "new-story"
                        else False
                    )
                    if (
                        str(feedback.get("recorded_at", "")) > recorded_at
                        and feedback.get("source") == "ask_user"
                        and feedback.get("disposition") == "current-sprint"
                        and feedback.get("responsible_scope") == "story"
                        and story_match
                    ):
                        feedback_matches = True
                        break
            if not before or state.get("requirements_sha256") in {None, before} or not amended or not feedback_matches:
                errors.append(
                    "Story 范围冲突尚未闭环；必须为受影响 Story 记录 ask_user 需求反馈、"
                    "更新 USER_STORIES 后执行 harness sprint amend: "
                    f"{receipt.get('from_task_id', path.stem)}"
                )
        if receipt.get("requires_structure_amend") is True:
            before = receipt.get("sprint_structure_sha256_before")
            expected_owner_types = set(receipt.get("expected_owner_types") or [])
            owner_added = any(
                item.get("structure_changed") is True
                and any(
                    (rows_by_id.get(str(task_id), {}).get("类型") or rows_by_id.get(str(task_id), {}).get("type"))
                    in expected_owner_types
                    for task_id in item.get("added") or []
                )
                for item in amendments
            )
            if (
                not before
                or state.get("structure_sha256") in {None, before}
                or not expected_owner_types
                or not owner_added
            ):
                errors.append(
                    "范围冲突缺少责任任务；必须补充责任阶段任务后执行 harness sprint amend: "
                    f"{receipt.get('responsible_scope', 'unknown')}"
                )
    return errors


def _git(root: Path, *args: str) -> tuple[bool, str]:
    outcome = execute(CommandSpec.argv_command(("git", *args), cwd=root))
    return outcome.ok, outcome.stdout.strip()


def linked_worktree(root: Path) -> bool:
    git_ok, git_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    common_ok, common_dir = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not git_ok or not common_ok:
        raise ValueError("当前目录不是可验证的 Git 工作区")
    return Path(git_dir).resolve() != Path(common_dir).resolve()


def legacy_unversioned_contract(state: dict[str, Any], contract: dict[str, Any]) -> bool:
    """Recognize only the explicitly migrated compatibility lane."""
    return (
        state.get("planning_contract_version") == 1
        and state.get("legacy_unversioned_contract") is True
        and contract.get("planning_contract_version") is None
    )


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


def validate_sprint_activation(
    root: Path,
    sprint_path: Path,
    *,
    require_completion_receipt: bool = True,
) -> list[str]:
    store = StateStore(root / ".harness/state/sprints")
    state = store.read_json(f"{sprint_path.stem}.json", None)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return ["Sprint 尚未通过 harness sprint activate 激活"]
    if ambiguous_fields := duplicate_sprint_contract_fields(sprint_path):
        return [f"Sprint 合同字段必须且只能声明一次: {ambiguous_fields}"]
    contract = sprint_planning_contract(sprint_path)
    plan_contract_version = contract.get("planning_contract_version")
    if "planning_contract_version" not in state:
        return [
            "旧 Sprint 激活状态缺少 planning contract；必须恢复激活时计划并执行 "
            "harness sprint migrate-state 记录兼容迁移"
        ]
    state_contract_version = state.get("planning_contract_version")
    if not legacy_unversioned_contract(state, contract) and plan_contract_version != state_contract_version:
        return ["Sprint planning contract 版本与激活状态不一致；不得通过修改计划文本升级或降级，必须显式迁移"]
    if state.get("structure_sha256") != sprint_structure_digest(sprint_path):
        return ["Sprint 结构已变化；必须执行 harness sprint amend 并记录原因"]
    if sprint_uses_story_requirements(sprint_header(sprint_path).get("sprint_type", ""), contract):
        current = sprint_source_requirements_digest(
            root / "USER_STORIES.md", sprint_path, contract.get("source_stories")
        )
        if not current or state.get("requirements_sha256") != current:
            return ["Sprint 引用的 USER_STORIES.md 已变化或无效；必须修正规划并执行 harness sprint amend"]
        if sprint_path.parent.name == "completed" and require_completion_receipt:
            completion_errors = validate_partial_sprint_completion(root, sprint_path, contract.get("source_stories"))
            if completion_errors:
                return completion_errors
        if contract.get("planning_contract_version") == 3:
            confirmations, confirmation_errors = validate_story_confirmations(root, contract.get("source_stories"))
            if confirmation_errors:
                return confirmation_errors
            if state.get("story_confirmations") != confirmations:
                return ["Sprint Story 确认凭证已变化；必须重新确认需求并执行 harness sprint amend"]
    return pending_task_reopen_errors(root, sprint_path, state)

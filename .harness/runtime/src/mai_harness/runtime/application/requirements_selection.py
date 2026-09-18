"""Bind the human Pick decision to exact Story content and the Sprint's AC subset."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.application.human_approval import record_decision, request_approval, require_approval
from mai_harness.runtime.domain.design_policy import policy_version
from mai_harness.runtime.domain.sprint_context import (
    sprint_planning_contract,
    user_story_records,
    validate_source_stories,
)
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def selection_payload(root: Path, plan: Path) -> dict[str, Any]:
    contract = sprint_planning_contract(plan)
    if policy_version(contract) != 2 or contract.get("requirement_mode") != "stories":
        raise ValueError("精确 US/AC 人工选择只适用于新版 stories 迭代")
    stories = root / "USER_STORIES.md"
    selected = contract.get("source_stories")
    digest, errors = validate_source_stories(stories, selected, allowed_statuses=frozenset({"draft", "ready", "done"}))
    if errors:
        raise ValueError("\n".join(errors))
    records, _, errors = user_story_records(stories)
    if errors:
        raise ValueError("\n".join(errors))
    full = [{"id": item["id"], "acs": sorted(records[item["id"]]["acs"])} for item in selected]
    content_digest, errors = validate_source_stories(
        stories, full, allowed_statuses=frozenset({"draft", "ready", "done"})
    )
    if errors:
        raise ValueError("\n".join(errors))
    return {
        "sprint": plan.stem,
        "source_stories": sorted(
            [{"id": item["id"], "acs": sorted(item["acs"])} for item in selected], key=lambda item: item["id"]
        ),
        "selected_sha256": digest,
        "story_content_sha256": content_digest,
    }


def prepare_selection(root: Path, plan: Path) -> dict[str, Any]:
    request = request_approval(root, "requirements-selection", selection_payload(root, plan))
    StateStore(root / ".harness/state/requirements/selections").write_json(plan.stem + ".json", request)
    return request


def confirm_selection(root: Path, plan: Path, event: dict[str, Any]) -> dict[str, Any]:
    reference = StateStore(root / ".harness/state/requirements/selections").read_json(plan.stem + ".json", {})
    if reference.get("payload") != selection_payload(root, plan):
        raise ValueError("US/AC 内容或选择已变化；先生成当前选择请求")
    return record_decision(root, reference, event)


def validate_selection(root: Path, plan: Path) -> list[str]:
    if policy_version(sprint_planning_contract(plan)) != 2:
        return []
    try:
        payload = selection_payload(root, plan)
        reference = StateStore(root / ".harness/state/requirements/selections").read_json(plan.stem + ".json", {})
        if reference.get("payload") != payload:
            return [
                "USER_STORIES.md 内容或本轮精确 US/AC 选择尚未人工确认或已变化；执行 requirements select 后呈现给用户"
            ]
        require_approval(root, reference)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    return []

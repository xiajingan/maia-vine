"""Application service for USER_STORIES intake and feedback synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mai_harness.runtime.domain.scope_routing import resolve_scope_route
from mai_harness.runtime.domain.sprint_context import (
    AC_ID,
    STORY_ID,
    planning_contract_digest,
    source_requirements_digest,
    sprint_header,
    sprint_planning_contract,
    sprint_uses_story_requirements,
    table_rows,
    user_story_records,
    validate_source_stories,
)
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import load_yaml

SCENE_FIELDS = {
    "user": "使用者",
    "context": "使用上下文 / 前置条件",
    "trigger": "触发事件",
    "current_difficulty": "当前做法与困难",
    "observable_outcome": "期望的可观察结果",
    "source": "来源 / 证据",
}
CONSTRAINT_FIELDS = {
    "business_rules": "必须保持的业务规则",
    "external_constraints": "外部约束",
    "non_goals": "非目标",
    "assumptions": "已知假设 / 待确认问题",
}
FEEDBACK_CLASSIFICATIONS = {
    "implementation-defect": "code",
    "requirement-change": "product",
    "new-story": "product",
    "non-product": "code",
}
LEGACY_STORY_FIELD = re.compile(r"(?m)^\s*-\s*\*\*关联 Story\*\*\s*[:：]\s*(.*?)\s*$")
LEGACY_STORY_PAYLOAD = re.compile(r"US-[0-9]+(?:\s*(?:,|，|/)\s*US-[0-9]+)*")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _story_sha256(
    path: Path,
    story_id: str,
    acs: list[str],
    *,
    allowed_statuses: frozenset[str] = frozenset({"ready"}),
) -> str:
    digest, errors = validate_source_stories(
        path,
        [{"id": story_id, "acs": acs}],
        allowed_statuses=allowed_statuses,
    )
    if errors or not digest:
        raise ValueError(f"Story {story_id} 无法生成确认摘要: {'; '.join(errors)}")
    return digest


def validate_story_confirmations(root: Path, references: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Bind selected ready Stories to explicit ask_user confirmation receipts."""
    path = root / "USER_STORIES.md"
    records, _, parse_errors = user_story_records(path)
    if parse_errors:
        return [], parse_errors
    selected = [item for item in references or [] if isinstance(item, dict)]
    confirmations = root / ".harness/state/requirements/confirmations"
    receipts: list[tuple[Path, dict[str, Any]]] = []
    if confirmations.exists():
        for receipt_path in sorted(confirmations.glob("*.json")):
            try:
                receipt = StateStore(confirmations).read_json(receipt_path.name, {})
            except (OSError, ValueError):
                continue
            if (
                isinstance(receipt, dict)
                and receipt.get("schema_version") == 1
                and receipt.get("source") == "ask_user"
                and receipt.get("status") == "ready"
                and str(receipt.get("confirmed_by", "")).strip()
                and isinstance(receipt.get("story_sha256"), dict)
                and isinstance(receipt.get("story_ids"), list)
            ):
                receipts.append((receipt_path, receipt))
    result: list[dict[str, str]] = []
    errors: list[str] = []
    for reference in selected:
        story_id = str(reference.get("id", ""))
        record = records.get(story_id)
        if record is None:
            errors.append(f"Story {story_id or '缺失'} 不存在，无法验证确认凭证")
            continue
        try:
            digest = _story_sha256(
                path,
                story_id,
                list(record.get("acs") or []),
                allowed_statuses=frozenset({"ready", "done"}),
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        matches = [
            (receipt_path, receipt)
            for receipt_path, receipt in receipts
            if receipt["story_sha256"].get(story_id) == digest and story_id in (receipt.get("story_ids") or [])
        ]
        if not matches:
            errors.append(
                f"Story {story_id} 缺少与当前内容一致的 ask_user 确认凭证；请执行 harness requirements confirm"
            )
            continue
        receipt_path, receipt = max(matches, key=lambda item: str(item[1].get("recorded_at", "")))
        result.append(
            {
                "story_id": story_id,
                "story_sha256": digest,
                "receipt": receipt_path.relative_to(root).as_posix(),
                "receipt_sha256": _sha(receipt_path),
            }
        )
    return sorted(result, key=lambda item: item["story_id"]), errors


def merge_story_confirmations(source_root: Path, target_root: Path, identifier: str) -> dict[str, Any]:
    """Merge current, content-bound confirmations from a completed worktree."""
    source = source_root / ".harness/state/requirements/confirmations"
    target_stories = target_root / "USER_STORIES.md"
    if not source.exists() or not target_stories.is_file():
        return {"schema_version": 1, "sprint": identifier, "imported": [], "status": "not-applicable"}
    records, _, parse_errors = user_story_records(target_stories)
    if parse_errors:
        raise ValueError("主工作区 USER_STORIES.md 无法汇合确认凭证:\n- " + "\n- ".join(parse_errors))
    destination = StateStore(target_root / ".harness/state/requirements/confirmations")
    imported: list[dict[str, str]] = []
    for path in sorted(source.glob("*.json")):
        receipt = StateStore(source).read_json(path.name, None)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("source") != "ask_user"
            or receipt.get("status") != "ready"
            or not isinstance(receipt.get("story_ids"), list)
            or not isinstance(receipt.get("story_sha256"), dict)
        ):
            raise ValueError(f"worktree Story 确认凭证格式非法: {path}")
        current_matches: list[str] = []
        for story_id in receipt["story_ids"]:
            record = records.get(story_id) if isinstance(story_id, str) else None
            if record is None:
                continue
            digest = _story_sha256(
                target_stories,
                story_id,
                list(record.get("acs") or []),
                allowed_statuses=frozenset({"ready", "done"}),
            )
            if receipt["story_sha256"].get(story_id) == digest:
                current_matches.append(story_id)
        if not current_matches:
            continue
        content = path.read_text(encoding="utf-8")
        existing = destination.path(path.name)
        if existing.is_file() and existing.read_text(encoding="utf-8") != content:
            raise ValueError(f"主工作区存在同名但内容冲突的 Story 确认凭证: {path.name}")
        if not existing.is_file():
            destination.write_text(path.name, content)
        imported.append(
            {
                "file": path.name,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "story_ids": ",".join(sorted(current_matches)),
            }
        )
    receipt = {
        "schema_version": 1,
        "sprint": identifier,
        "source_requirements_sha256": _sha(source_root / "USER_STORIES.md"),
        "target_requirements_sha256": _sha(target_stories),
        "imported": imported,
        "status": "merged",
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    StateStore(target_root / ".harness/state/requirements/confirmation-imports").write_json(
        f"{identifier}.json", receipt
    )
    return receipt


def _plain(value: Any, fallback: str = "TODO（待确认）") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if "\n" in text or "|" in text:
        raise ValueError("Story 字段不得包含换行或 Markdown 表格分隔符 |")
    return text


def load_story_input(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Story input 不存在: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Story input 无法读取: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Story input 顶层必须是对象")
    allowed = {"id", "title", "priority", "scene", "statement", "constraints", "acceptance"}
    if unknown := set(value) - allowed:
        raise ValueError(f"Story input 包含未知字段: {sorted(map(str, unknown))}")
    story_id = _plain(value.get("id"), "")
    if not STORY_ID.fullmatch(story_id):
        raise ValueError("Story input.id 必须使用 US-数字 格式")
    title = _plain(value.get("title"))
    priority = _plain(value.get("priority"), "P1").upper()
    if priority not in {"P0", "P1", "P2"}:
        raise ValueError("Story input.priority 必须为 P0/P1/P2")
    if "scene" in value and not isinstance(value.get("scene"), dict):
        raise ValueError("Story input.scene 必须是对象")
    if "constraints" in value and not isinstance(value.get("constraints"), dict):
        raise ValueError("Story input.constraints 必须是对象")
    scene_input = value.get("scene") or {}
    constraints_input = value.get("constraints") or {}
    if unknown := set(scene_input) - set(SCENE_FIELDS):
        raise ValueError(f"Story input.scene 包含未知字段: {sorted(map(str, unknown))}")
    if unknown := set(constraints_input) - set(CONSTRAINT_FIELDS):
        raise ValueError(f"Story input.constraints 包含未知字段: {sorted(map(str, unknown))}")
    scene = {key: _plain(scene_input.get(key)) for key in SCENE_FIELDS}
    constraints = {key: _plain(constraints_input.get(key)) for key in CONSTRAINT_FIELDS}
    acceptance_input = value.get("acceptance")
    acceptance: list[dict[str, str]] = []
    if acceptance_input is not None and not isinstance(acceptance_input, list):
        raise ValueError("Story input.acceptance 必须是数组")
    for index, item in enumerate(acceptance_input or []):
        if not isinstance(item, dict):
            raise ValueError(f"Story input.acceptance[{index}] 必须是对象")
        if set(item) - {"id", "given", "when", "then"}:
            raise ValueError(f"Story input.acceptance[{index}] 包含未知字段")
        ac_id = _plain(item.get("id"), "")
        if not AC_ID.fullmatch(ac_id) or not ac_id.startswith(f"{story_id}-AC-"):
            raise ValueError(f"Story input.acceptance[{index}].id 非法: {ac_id or '缺失'}")
        acceptance.append(
            {
                "id": ac_id,
                "given": _plain(item.get("given")),
                "when": _plain(item.get("when")),
                "then": _plain(item.get("then")),
            }
        )
    if len({item["id"] for item in acceptance}) != len(acceptance):
        raise ValueError("Story input.acceptance ID 不得重复")
    if not acceptance:
        acceptance.append(
            {
                "id": f"{story_id}-AC-01",
                "given": "TODO（待确认）",
                "when": "TODO（待确认）",
                "then": "TODO（待确认）",
            }
        )
    return {
        "id": story_id,
        "title": title,
        "priority": priority,
        "scene": scene,
        "statement": _plain(value.get("statement")),
        "constraints": constraints,
        "acceptance": acceptance,
    }


def render_story(story: dict[str, Any]) -> str:
    story_id = story["id"]
    scene_rows = "\n".join(f"| {label} | {story['scene'][key]} |" for key, label in SCENE_FIELDS.items())
    constraint_rows = "\n".join(f"- {label}：{story['constraints'][key]}" for key, label in CONSTRAINT_FIELDS.items())
    acceptance_rows = "\n".join(
        f"| {item['id']} | {item['given']} | {item['when']} | {item['then']} |" for item in story["acceptance"]
    )
    return (
        f"## {story_id} — {story['title']}\n\n"
        "### 场景内核\n\n"
        "| 字段 | 内容 |\n|------|------|\n"
        f"{scene_rows}\n\n"
        f"**用户故事**：{story['statement']}\n\n"
        "### 约束与非目标\n\n"
        f"{constraint_rows}\n\n"
        "### 行为验收示例\n\n"
        "| AC ID | Given | When | Then |\n|-------|-------|------|------|\n"
        f"{acceptance_rows}\n"
    )


def _replace_index(content: str, story: dict[str, Any], status: str) -> str:
    lines = content.splitlines()
    heading = next((index for index, line in enumerate(lines) if line.strip() == "## Story 索引"), -1)
    if heading < 0:
        raise ValueError("USER_STORIES.md 缺少 Story 索引")
    next_heading = next(
        (index for index in range(heading + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    header = next((index for index in range(heading + 1, next_heading) if lines[index].lstrip().startswith("|")), -1)
    if header < 0 or header + 1 >= len(lines):
        raise ValueError("USER_STORIES.md Story 索引缺少表格")
    end = header + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    row = f"| {story['id']} | {story['title']} | {story['priority']} | `{status}` |"
    replaced = False
    for index in range(header + 2, end):
        cells = [cell.strip().strip("`") for cell in lines[index].strip().strip("|").split("|")]
        if cells and cells[0] == story["id"]:
            lines[index] = row
            replaced = True
            break
    if not replaced:
        lines.insert(end, row)
    return "\n".join(lines).rstrip() + "\n"


def _replace_detail(content: str, story: dict[str, Any]) -> str:
    rendered = render_story(story).rstrip()
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(story['id'])}\s+[—-].*?(?=^##\s+|\Z)")
    matches = list(pattern.finditer(content))
    if len(matches) > 1:
        raise ValueError(f"USER_STORIES.md Story 详情 ID 重复: {story['id']}")
    if matches:
        match = matches[0]
        return content[: match.start()] + rendered + "\n\n" + content[match.end() :].lstrip("\n")
    marker = re.search(r"(?m)^## Story 编写规范\s*$", content)
    position = marker.start() if marker else len(content)
    return content[:position].rstrip() + "\n\n" + rendered + "\n\n" + content[position:].lstrip("\n")


def _replace_story_statuses(content: str, story_ids: set[str], status: str) -> str:
    lines = content.splitlines()
    heading = next((index for index, line in enumerate(lines) if line.strip() == "## Story 索引"), -1)
    if heading < 0:
        raise ValueError("USER_STORIES.md 缺少 Story 索引")
    next_heading = next(
        (index for index in range(heading + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    changed: set[str] = set()
    for index in range(heading + 1, next_heading):
        line = lines[index]
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[0].strip("`") not in story_ids:
            continue
        story_id = cells[0].strip("`")
        cells[3] = f"`{status}`"
        lines[index] = "| " + " | ".join(cells) + " |"
        changed.add(story_id)
    if changed != story_ids:
        raise ValueError(f"USER_STORIES.md 索引缺少待更新 Story: {sorted(story_ids - changed)}")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sync_story(
    root: Path,
    input_path: Path,
    *,
    status: str,
    action: str,
    confirmed_by: str = "",
    allow_existing: bool = False,
) -> dict[str, Any]:
    if status not in {"draft", "ready"}:
        raise ValueError("Story 同步状态只允许 draft/ready")
    if status == "ready" and not confirmed_by.strip():
        raise ValueError("Story 转为 ready 必须提供 confirmed_by")
    path = root / "USER_STORIES.md"
    story = load_story_input(input_path)
    records, _, parse_errors = user_story_records(path)
    if parse_errors:
        raise ValueError("USER_STORIES.md 无法安全更新:\n- " + "\n- ".join(parse_errors))
    existing = records.get(story["id"])
    if existing and existing["status"] == "done":
        raise ValueError("已完成 Story 不得静默改写；请创建新 Story")
    if existing and not allow_existing and existing["status"] != "draft":
        raise ValueError("intake 只能继续补全 draft Story；ready Story 变更必须走 feedback requirement-change")
    before = _sha(path)
    content = path.read_text(encoding="utf-8")
    candidate = _replace_detail(_replace_index(content, story, status), story)
    descriptor, temporary = tempfile.mkstemp(prefix=".USER_STORIES.validate.", suffix=".md", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        temporary_path.write_text(candidate, encoding="utf-8")
        if status == "ready":
            references = [{"id": story["id"], "acs": [item["id"] for item in story["acceptance"]]}]
            _, errors = validate_source_stories(temporary_path, references)
            if errors:
                raise ValueError("ready Story 未通过需求门禁:\n- " + "\n- ".join(errors))
    finally:
        temporary_path.unlink(missing_ok=True)
    _atomic_write(path, candidate)
    receipt = {
        "schema_version": 1,
        "action": action,
        "story_id": story["id"],
        "status": status,
        "confirmed_by": confirmed_by or None,
        "source": "ask_user" if confirmed_by else "agent-draft",
        "input_sha256": _sha(input_path),
        "before_sha256": before,
        "after_sha256": _sha(path),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    key = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:16]
    StateStore(root / ".harness/state/requirements/events").write_json(f"{key}.json", receipt)
    if status == "ready":
        record = user_story_records(path)[0][story["id"]]
        confirmation = {
            "schema_version": 1,
            "action": action,
            "story_ids": [story["id"]],
            "status": "ready",
            "confirmed_by": confirmed_by.strip(),
            "source": "ask_user",
            "story_sha256": {story["id"]: _story_sha256(path, story["id"], list(record.get("acs") or []))},
            "recorded_at": receipt["recorded_at"],
        }
        confirmation_key = hashlib.sha256(
            json.dumps(confirmation, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
        StateStore(root / ".harness/state/requirements/confirmations").write_json(
            f"{confirmation_key}.json", confirmation
        )
    return receipt


def confirm_stories(root: Path, story_ids: list[str], confirmed_by: str) -> dict[str, Any]:
    """Confirm or re-attest one or more complete Stories without rewriting semantics."""
    normalized = [story_id.strip() for story_id in story_ids if story_id.strip()]
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("Story 确认必须提供一个或多个不重复的 Story ID")
    if invalid := [story_id for story_id in normalized if not STORY_ID.fullmatch(story_id)]:
        raise ValueError(f"Story 确认包含非法 ID: {invalid}")
    if not confirmed_by.strip():
        raise ValueError("Story 确认必须提供 confirmed_by")
    path = root / "USER_STORIES.md"
    records, _, parse_errors = user_story_records(path)
    if parse_errors:
        raise ValueError("USER_STORIES.md 无法安全更新:\n- " + "\n- ".join(parse_errors))
    if missing := set(normalized) - set(records):
        raise ValueError(f"USER_STORIES.md 缺少待确认 Story: {sorted(missing)}")
    invalid_statuses = {
        story_id: records[story_id]["status"]
        for story_id in normalized
        if records[story_id]["status"] not in {"draft", "ready"}
    }
    if invalid_statuses:
        raise ValueError(f"只有 draft/ready Story 可以确认或重新确认: {invalid_statuses}")
    references = [{"id": story_id, "acs": list(records[story_id]["acs"])} for story_id in normalized]
    before_digest, errors = validate_source_stories(
        path,
        references,
        allowed_statuses=frozenset({"draft", "ready"}),
    )
    if errors:
        raise ValueError("draft Story 尚未满足确认条件:\n- " + "\n- ".join(errors))
    before = _sha(path)
    candidate = _replace_story_statuses(path.read_text(encoding="utf-8"), set(normalized), "ready")
    descriptor, temporary = tempfile.mkstemp(prefix=".USER_STORIES.confirm.", suffix=".md", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        temporary_path.write_text(candidate, encoding="utf-8")
        after_digest, after_errors = validate_source_stories(temporary_path, references)
        if after_errors or after_digest != before_digest:
            raise ValueError("Story draft → ready 确认未保持需求内容不变")
    finally:
        temporary_path.unlink(missing_ok=True)
    _atomic_write(path, candidate)
    receipt = {
        "schema_version": 1,
        "action": "confirm",
        "story_ids": normalized,
        "status": "ready",
        "confirmed_by": confirmed_by.strip(),
        "source": "ask_user",
        "before_sha256": before,
        "after_sha256": _sha(path),
        "requirements_sha256": after_digest,
        "story_sha256": {
            story_id: _story_sha256(path, story_id, list(records[story_id].get("acs") or [])) for story_id in normalized
        },
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    key = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    StateStore(root / ".harness/state/requirements/confirmations").write_json(f"{key}.json", receipt)
    return receipt


def _active_plan(root: Path, sprint: str) -> Path:
    plan = root / "docs/exec-plans/active" / f"{sprint}.md"
    if not plan.is_file():
        raise ValueError(f"反馈必须绑定 active Sprint: {sprint}")
    return plan


def _legacy_story_references(plan: Path, stories: Path) -> list[dict[str, Any]]:
    matches = LEGACY_STORY_FIELD.findall(plan.read_text(encoding="utf-8"))
    if not matches:
        raise ValueError("legacy Feature Sprint 必须声明唯一的关联 Story 字段")
    if len(matches) != 1:
        raise ValueError("legacy Feature Sprint 关联 Story 字段不得重复")
    payload = matches[0].strip()
    if not LEGACY_STORY_PAYLOAD.fullmatch(payload):
        raise ValueError("legacy Feature Sprint 关联 Story 只能包含明确的 US-数字 ID 列表")
    story_ids = re.findall(r"US-[0-9]+", payload)
    if len(story_ids) != len(set(story_ids)):
        raise ValueError("legacy Feature Sprint 关联 Story ID 不得重复")
    records, _, parse_errors = user_story_records(stories)
    if parse_errors:
        raise ValueError("USER_STORIES.md 无法安全解析:\n- " + "\n- ".join(parse_errors))
    references: list[dict[str, Any]] = []
    for story_id in story_ids:
        record = records.get(story_id)
        if record is None:
            raise ValueError(f"USER_STORIES.md 缺少关联 Story: {story_id}")
        acs = list(record["acs"])
        if not acs:
            raise ValueError(f"关联 Story {story_id} 缺少有效 AC")
        references.append({"id": story_id, "acs": acs})
    return references


def _sprint_story_references(plan: Path, stories: Path) -> tuple[bool, Any]:
    contract = sprint_planning_contract(plan)
    sprint_type = sprint_header(plan).get("sprint_type", "")
    if sprint_uses_story_requirements(sprint_type, contract):
        return True, contract.get("source_stories")
    if contract.get("planning_contract_version") is not None or sprint_type != "feature-sprint":
        return False, None
    return True, _legacy_story_references(plan, stories)


def _signoff_matches_planning_contract(plan: Path, digest: Any) -> bool:
    expected = planning_contract_digest(plan)
    contract = sprint_planning_contract(plan)
    if contract.get("planning_contract_version") is None:
        return isinstance(digest, str) and digest in {"", expected}
    return isinstance(digest, str) and digest == expected


def _signoff_candidates(root: Path, sprint: str) -> list[Path]:
    series_match = re.match(r"^sprint-\d+", sprint)
    candidates = [root / "docs/acceptance-reports" / f"{sprint}-boss-signoff.yml"]
    if series_match and series_match.group(0) != sprint:
        candidates.append(root / "docs/acceptance-reports" / f"{series_match.group(0)}-boss-signoff.yml")
    return candidates


def _approved_signoff(root: Path, plan: Path) -> tuple[Path, dict[str, Any]]:
    candidates = _signoff_candidates(root, plan.stem)
    signoff_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    try:
        signoff = yaml.safe_load(signoff_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Story 完成前必须存在可读取的 Boss signoff: {exc}") from exc
    series_match = re.match(r"^sprint-\d+", plan.stem)
    expected_sprints = {plan.stem, series_match.group(0) if series_match else plan.stem}
    if not isinstance(signoff, dict) or signoff.get("decision") != "approved":
        raise ValueError("Story 完成前 Boss signoff 必须 approved")
    if signoff.get("sprint") not in expected_sprints:
        raise ValueError("Story 完成前 Boss signoff 必须绑定当前 Sprint")
    if not _signoff_matches_planning_contract(plan, signoff.get("planning_contract_sha256")):
        raise ValueError("Story 完成前 Boss signoff 必须绑定当前 planning contract")
    return signoff_path, signoff


def _ac_semantic_sha256(record: dict[str, Any], ac_id: str) -> str:
    statement = re.findall(r"(?m)^\*\*用户故事\*\*\s*[:：]\s*(.+)$", record["raw"])
    constraints = {
        label: matches[0].strip() if len(matches) == 1 else ""
        for label in ("必须保持的业务规则", "外部约束", "非目标", "已知假设 / 待确认问题")
        for matches in [re.findall(rf"(?m)^-\s*{re.escape(label)}\s*[:：]\s*(.+)$", record["raw"])]
    }
    payload = {
        "story_id": ac_id.rsplit("-AC-", 1)[0],
        "title": record["title"],
        "scene": record["scene"],
        "statement": statement[0].strip() if len(statement) == 1 else "",
        "constraints": constraints,
        "acceptance": record["acs"].get(ac_id),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _selected_ac_semantics(records: dict[str, dict[str, Any]], references: Any) -> dict[str, dict[str, str]]:
    return {
        reference["id"]: {
            ac_id: _ac_semantic_sha256(records[reference["id"]], ac_id) for ac_id in reference.get("acs") or []
        }
        for reference in references or []
        if isinstance(reference, dict) and isinstance(reference.get("id"), str) and reference["id"] in records
    }


def _completion_receipt_path(root: Path, sprint: str) -> Path:
    return root / "docs/acceptance-reports" / f"{sprint}-requirements-completion.json"


def _delivered_story_acs(
    root: Path,
    stories: Path,
    story_id: str,
    current_sprint: str,
    current_acs: set[str],
) -> set[str]:
    """Derive AC delivery only from evidence bound to the current AC semantics."""
    records, _, errors = user_story_records(stories)
    if errors or story_id not in records:
        return set()
    delivered: set[str] = set()
    for completed in sorted((root / "docs/exec-plans/completed").glob("*.md")):
        try:
            applicable, references = _sprint_story_references(completed, stories)
            if not applicable:
                continue
            reference = next(
                (item for item in references or [] if isinstance(item, dict) and item.get("id") == story_id),
                None,
            )
            if reference is None:
                continue
            approved_path, _ = _approved_signoff(root, completed)
        except (OSError, UnicodeError, ValueError):
            continue
        selected = {value for value in reference.get("acs") or [] if isinstance(value, str)}
        if completed.stem == current_sprint:
            delivered.update(selected & current_acs)
            continue
        receipt_path = _completion_receipt_path(root, completed.stem)
        try:
            receipt = StateStore(receipt_path.parent).read_json(receipt_path.name, None)
        except (OSError, ValueError):
            continue
        semantics = receipt.get("delivered_ac_sha256") if isinstance(receipt, dict) else None
        story_semantics = semantics.get(story_id) if isinstance(semantics, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("sprint") != completed.stem
            or receipt.get("planning_contract_sha256") != planning_contract_digest(completed)
            or receipt.get("signoff") != approved_path.relative_to(root).as_posix()
            or receipt.get("signoff_sha256") != _sha(approved_path)
            or not isinstance(story_semantics, dict)
        ):
            continue
        delivered.update(
            ac_id for ac_id in selected if story_semantics.get(ac_id) == _ac_semantic_sha256(records[story_id], ac_id)
        )
    return delivered


def validate_partial_sprint_completion(root: Path, plan: Path, references: Any) -> list[str]:
    """Validate a completed Sprint whose selected Story remains ready due to partial AC delivery."""
    stories = root / "USER_STORIES.md"
    records, _, parse_errors = user_story_records(stories)
    if parse_errors:
        return parse_errors
    selected_ids = {
        item.get("id") for item in references or [] if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    ready_ids = {story_id for story_id in selected_ids if records.get(story_id, {}).get("status") == "ready"}
    if not ready_ids:
        return []
    receipt_path = _completion_receipt_path(root, plan.stem)
    try:
        receipt = StateStore(receipt_path.parent).read_json(receipt_path.name, None)
    except (OSError, ValueError) as exc:
        return [f"部分 AC completion receipt 无法读取: {exc}"]
    current_digest = source_requirements_digest(
        stories,
        references,
        allowed_statuses=frozenset({"ready", "done"}),
    )
    expected_semantics = _selected_ac_semantics(records, references)
    errors: list[str] = []
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        return [f"部分 AC Sprint 缺少合法 completion receipt: {receipt_path}"]
    if receipt.get("sprint") != plan.stem or receipt.get("status") != "partial":
        errors.append("部分 AC completion receipt 身份或状态不匹配")
    if set(receipt.get("partial_story_ids") or []) != ready_ids:
        errors.append("部分 AC completion receipt 未绑定仍为 ready 的 Story")
    if receipt.get("requirements_sha256") != current_digest:
        errors.append("部分 AC completion receipt 未绑定当前需求语义")
    if receipt.get("planning_contract_sha256") != planning_contract_digest(plan):
        errors.append("部分 AC completion receipt 未绑定当前 planning contract")
    if receipt.get("delivered_ac_sha256") != expected_semantics:
        errors.append("部分 AC completion receipt 未绑定本 Sprint 选中 AC 的当前语义")
    signoff = root / str(receipt.get("signoff", ""))
    if not signoff.is_file() or receipt.get("signoff_sha256") != _sha(signoff):
        errors.append("部分 AC completion receipt 未绑定当前 Boss signoff")
    else:
        try:
            approved_path, _ = _approved_signoff(root, plan)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if approved_path.resolve() != signoff.resolve():
                errors.append("部分 AC completion receipt 引用了非 canonical Boss signoff")
    return errors


def _append_source_story(plan: Path, story: dict[str, Any]) -> None:
    contract = sprint_planning_contract(plan)
    references = contract.get("source_stories")
    if not isinstance(references, list):
        raise ValueError("Sprint source_stories 必须是数组")
    if any(isinstance(item, dict) and item.get("id") == story["id"] for item in references):
        return
    references.append({"id": story["id"], "acs": [item["id"] for item in story["acceptance"]]})
    replacement = "source_stories: " + json.dumps(references, ensure_ascii=False, separators=(",", ":"))
    content, count = re.subn(r"(?m)^\s*source_stories\s*:.*$", replacement, plan.read_text(encoding="utf-8"))
    if count != 1:
        raise ValueError("Sprint source_stories 必须且只能声明一次")
    _atomic_write(plan, content)


def record_feedback(
    root: Path,
    sprint: str,
    classification: str,
    summary: str,
    confirmed_by: str,
    *,
    input_path: Path | None = None,
    disposition: str = "current-sprint",
) -> dict[str, Any]:
    if classification not in FEEDBACK_CLASSIFICATIONS:
        raise ValueError(f"反馈分类必须是 {sorted(FEEDBACK_CLASSIFICATIONS)} 之一")
    if not summary.strip() or not confirmed_by.strip():
        raise ValueError("反馈必须包含 summary 和 confirmed_by")
    if disposition not in {"current-sprint", "backlog"}:
        raise ValueError("反馈 disposition 必须为 current-sprint/backlog")
    if disposition == "backlog" and classification not in {"new-story", "non-product"}:
        raise ValueError("只有 new-story/non-product 反馈可以进入 backlog")
    plan = _active_plan(root, sprint)
    contract = sprint_planning_contract(plan)
    affected: list[str] = []
    story_receipt: dict[str, Any] | None = None
    if classification in {"requirement-change", "new-story"}:
        sprint_type = sprint_header(plan).get("sprint_type", "")
        if disposition == "current-sprint" and not sprint_uses_story_requirements(sprint_type, contract):
            raise ValueError("当前 Sprint 未绑定 Story；产品需求变化必须创建新的 story-bound Feature Sprint")
        planned_types = {row.get("类型") or row.get("type") for row in table_rows(plan.read_text(encoding="utf-8"))}
        feedback_owner_types: set[str] = set()
        feedback_owner: str | None = None
        if disposition == "current-sprint":
            rules = load_yaml(HarnessPaths.detect(project=root).rules / "task-rules.yml")
            route = resolve_scope_route(rules, sprint_type, "story")
            if transfer_to := route.get("transfer_to"):
                raise ValueError(f"当前 {sprint_type} 不承载 Story 变化；必须转入新的 {transfer_to}")
            feedback_owner_types = set(route.get("owner_tasks", ()))
            feedback_owner = next(
                (task_type for task_type in feedback_owner_types if task_type in planned_types),
                None,
            )
            if feedback_owner is None:
                raise ValueError("当前 Sprint 缺少规则指定的 Story 责任任务；请修正规划后再记录反馈")
        if input_path is None:
            raise ValueError(f"{classification} 必须提供 --story-input")
        story = load_story_input(input_path)
        selected = {item.get("id") for item in contract.get("source_stories") or [] if isinstance(item, dict)}
        if classification == "requirement-change" and story["id"] not in selected:
            raise ValueError("requirement-change 只能更新当前 Sprint 已选择的 Story")
        if classification == "new-story" and story["id"] in selected:
            raise ValueError("new-story 不得复用当前 Sprint 已选择的 Story ID")
        if (
            classification == "new-story"
            and disposition == "current-sprint"
            and (
                not isinstance(contract.get("source_stories"), list)
                or len(re.findall(r"(?m)^\s*source_stories\s*:", plan.read_text(encoding="utf-8"))) != 1
            )
        ):
            raise ValueError("Sprint source_stories 必须是唯一数组，修复计划后才能纳入新 Story")
        story_receipt = sync_story(
            root,
            input_path,
            status="ready",
            action=f"feedback:{classification}",
            confirmed_by=confirmed_by,
            allow_existing=classification == "requirement-change",
        )
        affected.append(story["id"])
        if classification == "new-story" and disposition == "current-sprint":
            _append_source_story(plan, story)
    rollback_to = (
        feedback_owner
        if disposition == "current-sprint" and classification in {"requirement-change", "new-story"}
        else FEEDBACK_CLASSIFICATIONS[classification]
        if disposition == "current-sprint"
        else None
    )
    receipt = {
        "schema_version": 1,
        "sprint": sprint,
        "classification": classification,
        "disposition": disposition,
        "summary": summary.strip(),
        "confirmed_by": confirmed_by.strip(),
        "source": "ask_user",
        "affected_story_ids": affected,
        "rollback_to": rollback_to,
        "responsible_scope": ("story" if classification in {"requirement-change", "new-story"} else None),
        "rollback_owner_types": (
            sorted(feedback_owner_types)
            if disposition == "current-sprint" and classification in {"requirement-change", "new-story"}
            else []
        ),
        "requires_amend": disposition == "current-sprint" and classification in {"requirement-change", "new-story"},
        "story_sync": story_receipt,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    key = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    StateStore(root / ".harness/state/requirements/feedback").write_json(f"{sprint}-{key}.json", receipt)
    return receipt


def complete_sprint_stories(root: Path, sprint: str) -> dict[str, Any]:
    """Perform the only automatic delivered lifecycle transition: ready -> done."""
    plan = root / "docs/exec-plans/completed" / f"{sprint}.md"
    if not plan.is_file():
        raise ValueError("Story 完成只能在 Sprint 计划归档到 completed 后执行")
    stories = root / "USER_STORIES.md"
    applicable, references = _sprint_story_references(plan, stories)
    if not applicable:
        return {
            "schema_version": 1,
            "sprint": sprint,
            "story_ids": [],
            "status": "not-applicable",
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    signoff_path, _ = _approved_signoff(root, plan)
    selected_digest, errors = validate_source_stories(
        stories,
        references,
        allowed_statuses=frozenset({"ready", "done"}),
    )
    if errors:
        raise ValueError("Sprint Story 无法完成:\n- " + "\n- ".join(errors))
    records, _, parse_errors = user_story_records(stories)
    if parse_errors:
        raise ValueError("USER_STORIES.md 无法安全解析:\n- " + "\n- ".join(parse_errors))
    selected_story_ids = {
        item["id"] for item in references or [] if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    selected_acs = {
        item["id"]: {value for value in item.get("acs") or [] if isinstance(value, str)}
        for item in references or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    delivered_acs = {
        story_id: _delivered_story_acs(root, stories, story_id, sprint, selected_acs[story_id])
        for story_id in selected_story_ids
    }
    story_ids = {
        story_id for story_id in selected_story_ids if set(records[story_id]["acs"]) <= delivered_acs[story_id]
    }
    for active in sorted((root / "docs/exec-plans/active").glob("*.md")):
        active_applicable, active_references = _sprint_story_references(active, stories)
        if not active_applicable:
            continue
        active_ids = {item.get("id") for item in active_references or [] if isinstance(item, dict)}
        if shared := story_ids & active_ids:
            raise ValueError(f"Story 仍被 active Sprint {active.stem} 引用，不能标记 done: {sorted(shared)}")
    original_content = stories.read_text(encoding="utf-8")
    before = hashlib.sha256(original_content.encode()).hexdigest()
    after_digest = selected_digest
    candidate = original_content
    if story_ids:
        candidate = _replace_story_statuses(original_content, story_ids, "done")
        descriptor, temporary = tempfile.mkstemp(prefix=".USER_STORIES.complete.", suffix=".md", dir=stories.parent)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            temporary_path.write_text(candidate, encoding="utf-8")
            after_digest, after_errors = validate_source_stories(
                temporary_path,
                references,
                allowed_statuses=frozenset({"ready", "done"}),
            )
            if after_errors or after_digest != selected_digest:
                raise ValueError("Story ready → done 后需求内容摘要发生变化")
        finally:
            temporary_path.unlink(missing_ok=True)
    receipt = {
        "schema_version": 1,
        "sprint": sprint,
        "story_ids": sorted(story_ids),
        "partial_story_ids": sorted(selected_story_ids - story_ids),
        "delivered_acs": {key: sorted(value) for key, value in delivered_acs.items()},
        "status": "done" if story_ids == selected_story_ids else "partial",
        "before_sha256": before,
        "after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "requirements_sha256": after_digest,
        "signoff": signoff_path.relative_to(root).as_posix(),
        "signoff_sha256": _sha(signoff_path),
        "planning_contract_sha256": planning_contract_digest(plan),
        "delivered_ac_sha256": _selected_ac_semantics(records, references),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    receipt_path = _completion_receipt_path(root, sprint)
    receipt_before = receipt_path.read_text(encoding="utf-8") if receipt_path.is_file() else None
    try:
        # Publish the evidence first so a failed receipt write can never leave a
        # Story in done without its completion contract. Both writes are atomic;
        # the snapshots below restore the pair if the second write fails.
        StateStore(receipt_path.parent).write_json(receipt_path.name, receipt)
        if story_ids:
            _atomic_write(stories, candidate)
    except Exception:
        if story_ids:
            _atomic_write(stories, original_content)
        if receipt_before is None:
            receipt_path.unlink(missing_ok=True)
        else:
            _atomic_write(receipt_path, receipt_before)
        raise
    return receipt

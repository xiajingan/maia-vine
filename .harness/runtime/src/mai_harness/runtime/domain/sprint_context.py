"""Pure parsing and policy helpers for Sprint context."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

SPRINT_ID = re.compile(r"^sprint-\d+-[a-z0-9][a-z0-9-]*$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LEGACY_PLANNING_CONTRACT_FIELDS = (
    "planning_contract_version",
    "delivery_strategy",
    "observable_outcomes",
    "boss_observation",
    "outcome_acceptance",
    "domain_coverage",
)
V2_PLANNING_CONTRACT_FIELDS = (
    "planning_contract_version",
    "source_stories",
    "impact_surfaces",
    *LEGACY_PLANNING_CONTRACT_FIELDS[1:],
)
PLANNING_CONTRACT_FIELDS = (
    "planning_contract_version",
    "requirement_mode",
    *V2_PLANNING_CONTRACT_FIELDS[1:],
)
SPRINT_HEADER_FIELDS = ("sprint_type", "base_ref", "base_sha", "branch")
OPTIONAL_PLANNING_CONTRACT_FIELDS = ("scope_transfer_from",)
BOOTSTRAP_SPRINT_TYPES = {"feature-sprint", "library-sprint"}
PLACEHOLDER = re.compile(r"(?i)\b(?:todo|tbd|placeholder)\b|待补|待定|占位|\{[^{}\n]+\}")
GENERIC_RESULT = re.compile(
    r"(?i)^(?:boss\s*)?(?:查看|检查|验证)?(?:系统)?(?:运行)?结果(?:符合预期)?$|^(?:一切|所有).*(?:正常|符合预期)$"
)
STORY_ID = re.compile(r"^US-[0-9]+$")
AC_ID = re.compile(r"^(US-[0-9]+)-AC-[0-9]+$")


def header_field(content: str, field: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*`?([^`\n]+?)`?\s*$", content)
    return match.group(1).strip() if match else ""


def header_list_field(content: str, field: str) -> list[str]:
    """Read a compact YAML-style list from a Markdown Sprint header."""

    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*\[([^]]*)]\s*$", content)
    return [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()] if match else []


def sprint_header(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    return {field: header_field(content, field) for field in SPRINT_HEADER_FIELDS}


def duplicate_sprint_contract_fields(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    version = _planning_value(content, "planning_contract_version")
    planning_fields = {
        1: LEGACY_PLANNING_CONTRACT_FIELDS,
        2: V2_PLANNING_CONTRACT_FIELDS,
        3: PLANNING_CONTRACT_FIELDS,
    }.get(version, ())
    return [
        field
        for field in (*SPRINT_HEADER_FIELDS, *planning_fields, *OPTIONAL_PLANNING_CONTRACT_FIELDS)
        if len(re.findall(rf"(?m)^\s*{re.escape(field)}\s*:", content)) > 1
    ]


def _planning_value(content: str, field: str) -> Any:
    raw = header_field(content, field)
    if not raw:
        return None
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return None


def sprint_planning_contract(path: Path) -> dict[str, Any]:
    return planning_contract_from_content(path.read_text(encoding="utf-8"))


def planning_contract_from_content(content: str) -> dict[str, Any]:
    return {field: _planning_value(content, field) for field in PLANNING_CONTRACT_FIELDS}


def canonical_planning_contract(path: Path) -> dict[str, Any]:
    return canonical_planning_contract_from_content(path.read_text(encoding="utf-8"))


def canonical_planning_contract_from_content(content: str) -> dict[str, Any]:
    contract = planning_contract_from_content(content)
    fields = {
        1: LEGACY_PLANNING_CONTRACT_FIELDS,
        2: V2_PLANNING_CONTRACT_FIELDS,
        3: PLANNING_CONTRACT_FIELDS,
    }.get(contract["planning_contract_version"], PLANNING_CONTRACT_FIELDS)
    canonical = {field: contract[field] for field in fields}
    for field in OPTIONAL_PLANNING_CONTRACT_FIELDS:
        if value := _planning_value(content, field):
            canonical[field] = value
    return canonical


def _concrete_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.strip()) >= 8
        and not PLACEHOLDER.search(value)
        and not GENERIC_RESULT.fullmatch(value.strip())
    )


def _meaningful_requirement(value: Any) -> bool:
    text = str(value or "").strip().strip("`")
    return bool(text) and "<!--" not in text and not PLACEHOLDER.search(text)


def _first_markdown_table(section: str) -> list[dict[str, str]]:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|") or index + 1 >= len(lines):
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        separator = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
        if len(headers) != len(separator) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            continue
        rows: list[dict[str, str]] = []
        for value in lines[index + 2 :]:
            if not value.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells, strict=True)))
        return rows
    return []


def _heading_section(content: str, heading: str, level: int) -> str:
    marker = "#" * level
    match = re.search(
        rf"(?ms)^{re.escape(marker)}\s+{re.escape(heading)}\s*$\n?(.*?)(?=^#{{1,{level}}}\s+|\Z)",
        content,
    )
    return match.group(1) if match else ""


def _heading_count(content: str, heading: str, level: int) -> int:
    marker = "#" * level
    return len(re.findall(rf"(?m)^{re.escape(marker)}\s+{re.escape(heading)}\s*$", content))


def user_story_records(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    """Parse the project-owned Markdown contract without inventing a second data file."""
    if not path.is_file():
        return {}, {}, [f"USER_STORIES.md 不存在: {path}"]
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {}, {}, [f"USER_STORIES.md 无法读取: {exc}"]
    errors: list[str] = []
    global_context = {
        heading: re.sub(r"\s+", " ", _heading_section(content, heading, 2)).strip()
        for heading in ("产品愿景", "产品策略与体验原则", "用户画像", "业务术语")
    }
    for heading in global_context:
        if _heading_count(content, heading, 2) > 1:
            errors.append(f"USER_STORIES.md 全局章节重复: {heading}")
    index_rows = _first_markdown_table(_heading_section(content, "Story 索引", 2))
    index: dict[str, dict[str, str]] = {}
    for row in index_rows:
        story_id = str(row.get("ID", "")).strip().strip("`")
        if STORY_ID.fullmatch(story_id):
            if story_id in index:
                errors.append(f"USER_STORIES.md Story 索引 ID 重复: {story_id}")
            index[story_id] = row
    matches = list(re.finditer(r"(?m)^##\s+(US-[0-9]+)\s+[—-]\s*(.*?)\s*$", content))
    records: dict[str, dict[str, Any]] = {}
    for match in matches:
        story_id, title = match.group(1), match.group(2).strip()
        if story_id in records:
            errors.append(f"USER_STORIES.md Story 详情 ID 重复: {story_id}")
        next_heading = re.search(r"(?m)^##\s+", content[match.end() :])
        end = match.end() + next_heading.start() if next_heading else len(content)
        raw = content[match.start() : end]
        for heading in ("场景内核", "约束与非目标", "行为验收示例"):
            if _heading_count(raw, heading, 3) != 1:
                errors.append(f"Story {story_id} 章节必须且只能声明一次: {heading}")
        scene = _first_markdown_table(_heading_section(raw, "场景内核", 3))
        scene_fields: dict[str, str] = {}
        for row in scene:
            field = str(row.get("字段", "")).strip()
            if not field:
                continue
            if field in scene_fields:
                errors.append(f"Story {story_id} 场景内核字段重复: {field}")
            scene_fields[field] = str(row.get("内容", "")).strip()
        acceptance = _first_markdown_table(_heading_section(raw, "行为验收示例", 3))
        acs: dict[str, dict[str, str]] = {}
        for row in acceptance:
            ac_id = str(row.get("AC ID", "")).strip().strip("`")
            if not ac_id:
                continue
            if ac_id in acs:
                errors.append(f"Story {story_id} AC ID 重复: {ac_id}")
            acs[ac_id] = row
        status = str(index.get(story_id, {}).get("状态", "")).strip().strip("`").casefold()
        records[story_id] = {
            "title": title,
            "status": status,
            "index": index.get(story_id, {}),
            "scene": scene_fields,
            "acs": acs,
            "raw": raw.strip(),
        }
    return records, global_context, errors


def validate_source_stories(
    path: Path,
    references: Any,
    *,
    allowed_statuses: frozenset[str] = frozenset({"ready"}),
) -> tuple[str | None, list[str]]:
    """Validate referenced Stories/ACs and return a lifecycle-stable input digest."""
    if not isinstance(references, list) or not references:
        return None, ["source_stories 必须是非空数组"]
    records, global_context, errors = user_story_records(path)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    required_scene = {
        "使用者",
        "使用上下文 / 前置条件",
        "触发事件",
        "当前做法与困难",
        "期望的可观察结果",
        "来源 / 证据",
    }
    required_constraints = ("必须保持的业务规则", "外部约束", "非目标", "已知假设 / 待确认问题")
    for index, reference in enumerate(references):
        if not isinstance(reference, dict) or set(reference) != {"id", "acs"}:
            errors.append(f"source_stories[{index}] 必须仅包含 id/acs")
            continue
        story_id, ac_ids = reference.get("id"), reference.get("acs")
        if not isinstance(story_id, str) or not STORY_ID.fullmatch(story_id):
            errors.append(f"source_stories[{index}].id 非法: {story_id}")
            continue
        if story_id in seen:
            errors.append(f"source_stories Story 重复: {story_id}")
            continue
        seen.add(story_id)
        if not isinstance(ac_ids, list) or not ac_ids or not all(isinstance(item, str) for item in ac_ids):
            errors.append(f"source_stories[{index}].acs 必须是非空 AC ID 数组")
            continue
        if len(ac_ids) != len(set(ac_ids)):
            errors.append(f"source_stories[{index}].acs 不得重复")
        record = records.get(story_id)
        if record is None:
            errors.append(f"USER_STORIES.md 缺少 Story: {story_id}")
            continue
        if record["status"] not in allowed_statuses:
            expected_status = "/".join(sorted(allowed_statuses))
            errors.append(f"Story {story_id} 状态必须为 {expected_status}，实际为 {record['status'] or '缺失'}")
        if not _meaningful_requirement(record["title"]):
            errors.append(f"Story {story_id} 标题仍是占位内容")
        missing_scene = required_scene - set(record["scene"])
        if missing_scene:
            errors.append(f"Story {story_id} 场景内核缺少字段: {sorted(missing_scene)}")
        for field in required_scene:
            if field in record["scene"] and not _meaningful_requirement(record["scene"][field]):
                errors.append(f"Story {story_id} 场景内核字段未完成: {field}")
        story_sentences = re.findall(r"(?m)^\*\*用户故事\*\*\s*[:：]\s*(.+)$", record["raw"])
        if len(story_sentences) != 1:
            errors.append(f"Story {story_id} 用户故事陈述必须且只能声明一次")
        elif not _meaningful_requirement(story_sentences[0]):
            errors.append(f"Story {story_id} 用户故事陈述未完成")
        for label in required_constraints:
            matches = re.findall(rf"(?m)^-\s*{re.escape(label)}\s*[:：]\s*(.+)$", record["raw"])
            if len(matches) != 1:
                errors.append(f"Story {story_id} 约束字段必须且只能声明一次: {label}")
            elif not _meaningful_requirement(matches[0]):
                errors.append(f"Story {story_id} 约束字段未完成: {label}")
        for ac_id, row in record["acs"].items():
            if not AC_ID.fullmatch(ac_id) or not ac_id.startswith(f"{story_id}-AC-"):
                errors.append(f"Story {story_id} 包含非法 AC ID: {ac_id}")
            for field in ("Given", "When", "Then"):
                if not _meaningful_requirement(row.get(field)):
                    errors.append(f"AC {ac_id} 字段未完成: {field}")
        unknown = set(ac_ids) - set(record["acs"])
        if unknown:
            errors.append(f"Story {story_id} 未定义引用的 AC: {sorted(unknown)}")
        # Status is lifecycle metadata, not requirement content. Canonicalize an
        # allowed delivered Story to its planning state so ready -> done keeps
        # the activated digest while every semantic Story/AC edit still drifts.
        selected.append({"id": story_id, "status": "ready", "acs": ac_ids, "story": record["raw"]})
    digest = hashlib.sha256(
        json.dumps(
            {"global_context": global_context, "selected_stories": selected},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return digest, errors


def source_requirements_digest(
    path: Path,
    references: Any,
    *,
    allowed_statuses: frozenset[str] = frozenset({"ready"}),
) -> str | None:
    digest, errors = validate_source_stories(path, references, allowed_statuses=allowed_statuses)
    return digest if not errors else None


def sprint_source_requirements_digest(stories_path: Path, sprint_path: Path, references: Any) -> str | None:
    """Keep semantic identity stable across ready/done lifecycle transitions."""
    allowed_statuses = frozenset({"ready", "done"}) if sprint_path.parent.name == "completed" else frozenset({"ready"})
    return source_requirements_digest(stories_path, references, allowed_statuses=allowed_statuses)


def validate_user_stories(path: Path) -> list[str]:
    """Validate the complete project requirement source, not only one Sprint slice."""
    records, global_context, errors = user_story_records(path)
    if not path.is_file():
        return errors
    content = path.read_text(encoding="utf-8")
    for heading in global_context:
        count = _heading_count(content, heading, 2)
        if count != 1:
            errors.append(f"USER_STORIES.md 全局章节必须且只能声明一次: {heading}")
    if _heading_count(content, "Story 索引", 2) != 1:
        errors.append("USER_STORIES.md Story 索引必须且只能声明一次")
    index_rows = _first_markdown_table(_heading_section(content, "Story 索引", 2))
    index_ids: set[str] = set()
    for row in index_rows:
        story_id = str(row.get("ID", "")).strip().strip("`")
        if not STORY_ID.fullmatch(story_id):
            errors.append(f"USER_STORIES.md Story 索引包含非法 ID: {story_id or '缺失'}")
            continue
        index_ids.add(story_id)
        priority = str(row.get("优先级", "")).strip().strip("`").upper()
        status = str(row.get("状态", "")).strip().strip("`").casefold()
        if priority not in {"P0", "P1", "P2"}:
            errors.append(f"Story {story_id} 优先级必须为 P0/P1/P2")
        if status not in {"draft", "ready", "done", "deferred"}:
            errors.append(f"Story {story_id} 状态非法: {status or '缺失'}")
        if status in {"ready", "done"} and story_id in records:
            index_title = str(row.get("标题", "")).strip()
            if index_title != records[story_id]["title"]:
                errors.append(f"Story {story_id} 索引标题与详情标题不一致")
    detail_ids = set(records)
    if missing_details := index_ids - detail_ids:
        errors.append(f"USER_STORIES.md 索引 Story 缺少详情: {sorted(missing_details)}")
    if missing_index := detail_ids - index_ids:
        errors.append(f"USER_STORIES.md 详情 Story 缺少索引: {sorted(missing_index)}")
    references = [
        {"id": story_id, "acs": list(record["acs"])}
        for story_id, record in records.items()
        if record["status"] in {"ready", "done"}
    ]
    if references:
        _, selected_errors = validate_source_stories(
            path,
            references,
            allowed_statuses=frozenset({"ready", "done"}),
        )
        errors.extend(selected_errors)
    return list(dict.fromkeys(errors))


def sprint_uses_story_requirements(sprint_type: str, contract: dict[str, Any]) -> bool:
    """Return whether a contract is bound to USER_STORIES, preserving v2 compatibility."""
    version = contract.get("planning_contract_version")
    return (version == 2 and sprint_type == "feature-sprint") or (
        version == 3 and contract.get("requirement_mode") == "stories"
    )


def validate_product_trace(paths: list[Path], references: Any, architecture_path: Path | None = None) -> list[str]:
    """Validate the PRD's canonical scope, trace, translation, and complexity contract."""
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    for path in paths:
        if path.name == "index.md" or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"PRD 无法读取: {path}: {exc}")
            continue
        sections = list(re.finditer(r"(?ms)^#{2,4}\s+最小范围与追溯矩阵\s*$\n?(.*?)(?=^#{1,4}\s+|\Z)", content))
        if len(sections) != 1:
            errors.append(f"PRD 必须且只能声明一个『最小范围与追溯矩阵』章节: {path}")
            continue
        rows.extend(_first_markdown_table(sections[0].group(1)))
    if errors:
        return errors
    required_columns = {
        "Scope ID",
        "Scope Key",
        "模块",
        "页面/功能区域",
        "范围动作",
        "转译",
        "来源",
        "本次产品行为",
        "新增复杂度",
        "必要性 / 未复用原因",
        "验收观察",
    }
    if not rows or not required_columns <= set(rows[0]):
        return [f"PRD 最小范围与追溯矩阵必须包含列: {sorted(required_columns)}"]
    expected = {
        identifier
        for reference in references or []
        if isinstance(reference, dict)
        for identifier in [reference.get("id"), *(reference.get("acs") or [])]
        if isinstance(identifier, str)
    }
    covered: set[str] = set()
    scope_ids: set[str] = set()
    architecture_headings: set[str] = set()
    if architecture_path is not None and architecture_path.is_file():
        try:
            architecture_content = architecture_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return [f"ARCHITECTURE.md 无法读取: {architecture_path}: {exc}"]
        architecture_headings = {
            match.group(1).strip().rstrip("#").strip()
            for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", architecture_content)
        }
    allowed_actions = {"保持", "修改", "新增", "删除"}
    allowed_translations = {"保持", "细化", "变更"}
    allowed_complexity = {"页面", "步骤", "角色", "状态", "外部依赖", "业务规则"}
    for row in rows:
        scope_id = str(row.get("Scope ID", "")).strip().strip("`")
        if not re.fullmatch(r"SCOPE-[0-9]+", scope_id):
            errors.append(f"PRD 范围项 Scope ID 非法: {scope_id or '缺失'}")
        elif scope_id in scope_ids:
            errors.append(f"PRD 范围项 Scope ID 重复: {scope_id}")
        scope_ids.add(scope_id)
        action = str(row.get("范围动作", "")).strip().strip("`")
        translation = str(row.get("转译", "")).strip().strip("`")
        if action not in allowed_actions:
            errors.append(f"PRD {scope_id or '范围项'} 范围动作必须是 {sorted(allowed_actions)} 之一")
        if translation not in allowed_translations:
            errors.append(f"PRD {scope_id or '范围项'} 转译必须是 {sorted(allowed_translations)} 之一")
        source = str(row.get("来源", "")).strip()
        identifiers = set(re.findall(r"US-[0-9]+(?:-AC-[0-9]+)?", source))
        architecture_sources = {
            value.strip() for value in re.findall(r"(?:^|[,，;；])\s*ARCH:([^,，;；]+)", source) if value.strip()
        }
        invalid_architecture_sources = architecture_sources - architecture_headings
        if invalid_architecture_sources:
            errors.append(
                f"PRD {scope_id or '范围项'} 引用了 ARCHITECTURE.md 不存在的章节: "
                f"{sorted(invalid_architecture_sources)}"
            )
        valid_sources = bool(identifiers & expected) if expected else bool(architecture_sources & architecture_headings)
        if not valid_sources:
            errors.append(
                f"PRD {scope_id or '范围项'} 必须绑定选中的 Story/AC；ARCH:<ARCHITECTURE.md 章节> 只能作为附加约束"
                if expected
                else f"PRD {scope_id or '范围项'} 必须绑定 ARCH:<ARCHITECTURE.md 章节>"
            )
        complexity = str(row.get("新增复杂度", "")).strip().strip("`")
        complexity_added = bool(complexity) and complexity != "无"
        if not complexity:
            errors.append(f"PRD {scope_id or '范围项'} 新增复杂度必须明确填写『无』或分类项")
        elif complexity_added:
            items = [item.strip() for item in re.split(r"[;；]", complexity) if item.strip()]
            for item in items:
                match = re.fullmatch(r"([^:：]+)\s*[:：]\s*(.+)", item)
                if (
                    match is None
                    or match.group(1).strip() not in allowed_complexity
                    or not _meaningful_requirement(match.group(2))
                ):
                    errors.append(
                        f"PRD {scope_id or '范围项'} 新增复杂度必须使用 "
                        f"{sorted(allowed_complexity)}:具体内容，或填写『无』"
                    )
        if (action in {"新增", "修改", "删除"} or complexity_added) and not _concrete_text(
            row.get("必要性 / 未复用原因")
        ):
            errors.append(f"PRD {scope_id or '范围项'} 缺少具体的必要性或未复用原因")
        if not _concrete_text(row.get("本次产品行为")):
            errors.append(f"PRD {scope_id or '范围项'} 存在未完成或泛化的产品行为")
        if not _concrete_text(row.get("验收观察")):
            errors.append(f"PRD {scope_id or '范围项'} 存在未完成或泛化的验收观察")
        covered.update(identifiers)
    if missing := expected - covered:
        errors.append(f"PRD 最小范围与追溯矩阵未覆盖 Sprint 需求输入: {sorted(missing)}")
    if unknown := covered - expected:
        errors.append(f"PRD 最小范围与追溯矩阵包含 Sprint 未选择的需求输入: {sorted(unknown)}")
    return errors


def architecture_domains(path: Path) -> set[str]:
    """Read domain IDs from the first table below ARCHITECTURE.md's domain section."""
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    in_section = False
    for index, line in enumerate(lines):
        if re.match(r"^##\s+(?:领域(?:划分)?(?:与数据所有权)?|Domain(?:s| Model)?)\s*$", line.strip(), re.I):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.lstrip().startswith("|") or index + 1 >= len(lines):
            continue
        headers = [cell.strip().casefold() for cell in line.strip().strip("|").split("|")]
        if not headers or headers[0] not in {"域", "领域", "领域/能力", "domain", "domain id"}:
            continue
        domains: set[str] = set()
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|"):
                break
            value = row.strip().strip("|").split("|", 1)[0].strip().strip("`")
            if value and "<!--" not in value and not PLACEHOLDER.search(value):
                domains.add(value.casefold())
        return domains
    return set()


def planning_contract_digest(path: Path) -> str:
    payload = json.dumps(canonical_planning_contract(path), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def has_established_legacy_sprint_metadata(root: Path) -> bool:
    """Return whether an old project has verifiable Sprint execution history."""
    state_root = root / ".harness/state/sprints"
    for path in state_root.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(state, dict)
            and state.get("schema_version") == 1
            and (
                state.get("planning_contract_version") == 1
                or "planning_contract_version" not in state
                or state.get("legacy_unversioned_contract") is True
            )
        ):
            return True
    completed_root = root / "docs/exec-plans/completed"
    for path in completed_root.glob("sprint-*.md"):
        try:
            content = path.read_text(encoding="utf-8")
            contract = sprint_planning_contract(path)
        except (OSError, UnicodeError):
            continue
        if content.strip() and contract.get("planning_contract_version") in {None, 1}:
            return True
    return False


def bootstrap_candidate_digest(receipt: dict[str, Any]) -> str:
    payload = {field: receipt.get(field) for field in ("domains", "architecture_sha256", "evidence")}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_bootstrap_completion(
    root: Path,
    planning: dict[str, Any],
    architecture_path: Path,
    receipt_override: dict[str, Any] | None = None,
    *,
    require_signoff: bool = True,
    delivery_strategies: dict[str, Any] | None = None,
    sprint_delivery_contracts: dict[str, Any] | None = None,
    sprint_requirement_modes: dict[str, Any] | None = None,
) -> list[str]:
    """Require an auditable receipt before an explicitly greenfield project exits bootstrap."""
    if planning.get("project_stage") != "established":
        return []
    relative = planning.get("bootstrap_completion_receipt")
    candidate = Path(relative) if isinstance(relative, str) else Path()
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        return ["planning.bootstrap_completion_receipt 必须是安全的工程内相对路径"]
    receipt_path = root / candidate
    if receipt_override is None and not receipt_path.is_file():
        if has_established_legacy_sprint_metadata(root):
            return []
        return [f"bootstrap completion receipt 不存在: {receipt_path}"]
    if receipt_override is None:
        try:
            receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return [f"bootstrap completion receipt 无法读取: {exc}"]
    else:
        receipt = receipt_override
    if not isinstance(receipt, dict):
        return ["bootstrap completion receipt 顶层必须是对象"]
    errors: list[str] = []
    expected_fields = {"domains", "architecture_sha256", "evidence", "candidate_sha256"}
    if require_signoff:
        expected_fields.add("signoff")
    if set(receipt) != expected_fields:
        errors.append(f"bootstrap completion receipt 字段必须严格为: {sorted(expected_fields)}")
    validate_current_architecture = receipt_override is not None
    current_domains = architecture_domains(architecture_path)
    receipt_domains = receipt.get("domains")
    if not isinstance(receipt_domains, list) or not all(isinstance(item, str) for item in receipt_domains):
        errors.append("bootstrap completion receipt.domains 必须是字符串数组")
        receipt_domains = []
    baseline_domains = {item.casefold() for item in receipt_domains}
    if validate_current_architecture and baseline_domains != current_domains:
        errors.append("bootstrap completion receipt 未覆盖当前 ARCHITECTURE.md 全部领域")
    architecture_sha = hashlib.sha256(architecture_path.read_bytes()).hexdigest() if architecture_path.is_file() else ""
    if validate_current_architecture and receipt.get("architecture_sha256") != architecture_sha:
        errors.append("bootstrap completion receipt 未绑定当前 ARCHITECTURE.md")
    candidate_sha = bootstrap_candidate_digest(receipt)
    if receipt.get("candidate_sha256") != candidate_sha:
        errors.append("bootstrap completion receipt 候选摘要无效")
    evidence = receipt.get("evidence")
    covered_domains: set[str] = set()
    if not isinstance(evidence, list) or not evidence:
        errors.append("bootstrap completion receipt.evidence 必须是非空 outcome receipt 数组")
    else:
        for index, value in enumerate(evidence):
            if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
                errors.append(f"bootstrap completion receipt.evidence[{index}] 必须包含 path/sha256")
                continue
            relative = value.get("path")
            path = Path(relative) if isinstance(relative, str) else Path()
            evidence_path = root / path
            if (
                not isinstance(relative, str)
                or not relative
                or path.is_absolute()
                or ".." in path.parts
                or not evidence_path.is_file()
                or not evidence_path.resolve().is_relative_to(root.resolve())
                or not path.name.endswith("-outcome.yml")
            ):
                errors.append(f"bootstrap completion evidence 必须是工程内 outcome receipt 文件: {relative}")
                continue
            content = evidence_path.read_bytes()
            if value.get("sha256") != hashlib.sha256(content).hexdigest():
                errors.append(f"bootstrap completion evidence 内容已变化: {relative}")
                continue
            try:
                outcome = yaml.safe_load(content)
            except yaml.YAMLError as exc:
                errors.append(f"bootstrap completion evidence 无法读取: {relative}: {exc}")
                continue
            if (
                not isinstance(outcome, dict)
                or outcome.get("result") != "passed"
                or not isinstance(outcome.get("producer"), dict)
                or not isinstance(outcome.get("evidence"), list)
                or not outcome["evidence"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(outcome.get("planning_contract_sha256", "")))
            ):
                errors.append(f"bootstrap completion evidence 不是已认证的 Sprint outcome: {relative}")
                continue
            sprint_id = outcome.get("sprint")
            canonical_outcome = sprint_outcome_path(root, str(sprint_id)) if isinstance(sprint_id, str) else None
            if canonical_outcome is None or evidence_path.resolve() != canonical_outcome.resolve():
                errors.append(f"bootstrap completion evidence 必须引用 canonical Sprint outcome: {relative}")
                continue
            plan = next(
                (
                    root / "docs/exec-plans" / status / f"{sprint_id}.md"
                    for status in ("active", "completed")
                    if isinstance(sprint_id, str) and (root / "docs/exec-plans" / status / f"{sprint_id}.md").is_file()
                ),
                None,
            )
            if plan is None:
                errors.append(f"bootstrap completion evidence 缺少对应 Sprint 计划: {sprint_id}")
                continue
            producer = outcome.get("producer") or {}
            source_sprint_type = sprint_header(plan)["sprint_type"]
            if source_sprint_type not in BOOTSTRAP_SPRINT_TYPES:
                errors.append(f"bootstrap completion evidence Sprint 类型非法: {source_sprint_type}")
                continue
            source_contract = (sprint_delivery_contracts or {}).get(source_sprint_type, {})
            producer_kind = source_contract.get("outcome_producer") if isinstance(source_contract, dict) else None
            if producer_kind not in {"boss-signoff", "task-review"}:
                errors.append(f"bootstrap completion 缺少 {source_sprint_type} 的可信 producer 规则")
                continue
            if producer_kind == "task-review":
                planned_rows = table_rows(plan.read_text(encoding="utf-8"))
                producer_task_id = producer.get("task_id") if isinstance(producer, dict) else None
                producer_task_type = producer.get("task_type") if isinstance(producer, dict) else None
                matching_producers = [
                    row
                    for row in planned_rows
                    if _row_value(row, "id") == producer_task_id
                    and _row_value(row, "类型", "type") == producer_task_type
                ]
                terminal_tasks = set(source_contract.get("terminal_tasks", []))
                if len(matching_producers) != 1 or producer_task_type not in terminal_tasks:
                    errors.append("bootstrap completion task-review producer 必须是计划内唯一终态任务")
                    continue
            if validate_current_architecture:
                planning_errors = validate_sprint_planning_contract(
                    plan,
                    table_rows(plan.read_text(encoding="utf-8")),
                    {
                        "project_stage": "greenfield",
                        "minimum_bootstrap_domains": planning.get("minimum_bootstrap_domains"),
                    },
                    architecture_path,
                    delivery_strategies,
                    sprint_delivery_contracts,
                    sprint_requirement_modes=sprint_requirement_modes,
                )
                if planning_errors:
                    errors.append(f"bootstrap completion evidence planning contract 无效: {planning_errors}")
                    continue
            outcome_errors = validate_sprint_outcome_evidence(
                root,
                plan,
                producer.get("task_id") if isinstance(producer, dict) else None,
                producer.get("task_type") if isinstance(producer, dict) else None,
                producer_kind,
                outcome,
            )
            if outcome_errors:
                errors.append(f"bootstrap completion evidence 未通过 outcome 门禁: {relative}: {outcome_errors}")
                continue
            contract = sprint_planning_contract(plan)
            if contract.get("delivery_strategy") != "domain-bootstrap":
                errors.append(f"bootstrap completion evidence 不是 domain-bootstrap Sprint: {relative}")
                continue
            coverage = contract.get("domain_coverage")
            if isinstance(coverage, dict):
                covered_domains.update(str(domain).casefold() for domain in coverage)
        if covered_domains != baseline_domains:
            errors.append("bootstrap completion outcome 未覆盖签署时的全部基线领域")
    if require_signoff:
        signoff_relative = receipt.get("signoff")
        signoff_path = Path(signoff_relative) if isinstance(signoff_relative, str) else Path()
        if (
            not isinstance(signoff_relative, str)
            or not signoff_relative
            or signoff_path.is_absolute()
            or ".." in signoff_path.parts
            or not (root / signoff_path).resolve().is_relative_to(root.resolve())
            or signoff_path != (root / signoff_path).resolve().relative_to(root.resolve())
            or not (root / signoff_path).is_file()
            or (root / signoff_path).resolve() == receipt_path.resolve()
        ):
            errors.append("bootstrap completion receipt 缺少独立且安全的外部 Boss signoff")
        else:
            try:
                signoff = yaml.safe_load((root / signoff_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                errors.append(f"bootstrap completion Boss signoff 无法读取: {exc}")
                signoff = None
            if (
                not isinstance(signoff, dict)
                or signoff.get("decision") != "approved"
                or signoff.get("source") != "ask_user"
                or signoff.get("candidate_sha256") != candidate_sha
                or not isinstance(signoff.get("confirmed_by"), str)
                or not signoff["confirmed_by"].strip()
                or not isinstance(signoff.get("confirmed_at"), str)
                or len(signoff["confirmed_at"].strip()) < 8
            ):
                errors.append("bootstrap completion Boss signoff 未批准当前候选摘要")
    return errors


def sprint_outcome_path(root: Path, sprint_id: str) -> Path:
    return root / "docs/acceptance-reports" / f"{sprint_id}-outcome.yml"


def build_sprint_outcome_receipt(
    root: Path,
    plan: Path,
    evidence_paths: list[str],
    producer: dict[str, str],
    attestation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build a receipt from a producer attestation or an interactive Boss walkthrough record."""
    errors: list[str] = []
    for relative in evidence_paths:
        path = Path(relative)
        resolved = (root / path).resolve()
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or not resolved.is_relative_to(root.resolve())
            or path != resolved.relative_to(root.resolve())
            or not resolved.is_file()
        ):
            errors.append(f"Sprint outcome evidence 路径无效或不存在: {relative}")
            continue
        try:
            resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            errors.append(f"Sprint outcome evidence 必须引用可读取的文本证据: {relative}")
    evidence: list[dict[str, str]] = []
    if attestation is not None:
        if (
            not isinstance(attestation, dict)
            or attestation.get("planning_contract_sha256") != planning_contract_digest(plan)
            or attestation.get("result") != "passed"
            or not isinstance(attestation.get("evidence"), list)
        ):
            errors.append("Sprint outcome producer attestation 未绑定当前 planning contract")
        else:
            evidence = attestation["evidence"]
        supplied = set(evidence_paths)
        attested = {
            item.get("path") for item in evidence if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if supplied != attested:
            errors.append("Sprint outcome CLI evidence 与 producer attestation 不一致")
    else:
        errors.append("Sprint outcome 必须由可信 producer 提供结构化 attestation")
    return (
        {
            "sprint": plan.stem,
            "planning_contract_sha256": planning_contract_digest(plan),
            "result": "passed",
            "producer": producer,
            "evidence": evidence,
        },
        errors,
    )


def validate_sprint_outcome_evidence(
    root: Path,
    plan: Path,
    expected_task_id: str | None = None,
    expected_task_type: str | None = None,
    expected_producer_kind: str | None = None,
    receipt_override: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the common, auditable output receipt used by every Sprint flow."""
    contract = sprint_planning_contract(plan)
    if contract["planning_contract_version"] is None:
        return []
    receipt_path = sprint_outcome_path(root, plan.stem)
    if receipt_override is None and not receipt_path.is_file():
        return [f"Sprint outcome evidence 不存在: {receipt_path}"]
    if receipt_override is None:
        try:
            receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return [f"Sprint outcome evidence 无法读取: {receipt_path}: {exc}"]
    else:
        receipt = receipt_override
    if not isinstance(receipt, dict):
        return [f"Sprint outcome evidence 顶层必须是对象: {receipt_path}"]
    errors: list[str] = []
    if receipt.get("sprint") != plan.stem:
        errors.append(f"Sprint outcome evidence 身份不匹配: {receipt.get('sprint')} != {plan.stem}")
    if receipt.get("planning_contract_sha256") != planning_contract_digest(plan):
        errors.append("Sprint outcome evidence 未绑定当前 planning contract")
    if receipt.get("result") != "passed":
        errors.append("Sprint outcome evidence result 必须为 passed")
    evidence = receipt.get("evidence")
    producer_attestation = {
        "planning_contract_sha256": planning_contract_digest(plan),
        "result": "passed",
        "evidence": evidence,
    }
    producer = receipt.get("producer")
    if not isinstance(producer, dict):
        errors.append("Sprint outcome evidence.producer 必须是对象")
    elif expected_producer_kind is not None and producer.get("kind") != expected_producer_kind:
        errors.append(
            f"Sprint outcome evidence producer 必须由 {expected_producer_kind} 生成，实际为 {producer.get('kind')}"
        )
    elif producer.get("kind") == "task-review":
        if producer.get("task_id") != expected_task_id or producer.get("task_type") != expected_task_type:
            errors.append("Sprint outcome evidence producer 与当前终态任务不匹配")
        state_relative = producer.get("attempt_state_path")
        state_path = Path(state_relative) if isinstance(state_relative, str) else Path()
        attempt_state: dict[str, Any] | None = None
        if (
            not isinstance(state_relative, str)
            or not state_relative
            or state_path.is_absolute()
            or ".." in state_path.parts
            or not (root / state_path).resolve().is_relative_to(root.resolve())
            or not (root / state_path).is_file()
        ):
            errors.append("Sprint outcome evidence producer.attempt_state_path 无效或不存在")
        else:
            canonical_state = root / ".harness/state/tasks" / f"task-{plan.stem}--{expected_task_id}.json"
            if (root / state_path).resolve() != canonical_state.resolve():
                errors.append("Sprint outcome evidence producer 未绑定 canonical current attempt")
            try:
                loaded_state = json.loads((root / state_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"Sprint outcome evidence producer attempt state 无法读取: {exc}")
            else:
                attempt_state = loaded_state if isinstance(loaded_state, dict) else None
                review_state = attempt_state.get("review") if attempt_state else None
                if (
                    not isinstance(review_state, dict)
                    or attempt_state.get("schema_version") != 3
                    or attempt_state.get("status") != "ready"
                    or attempt_state.get("run_id") != producer.get("run_id")
                    or attempt_state.get("task_id") != expected_task_id
                    or attempt_state.get("task_type") != expected_task_type
                    or review_state.get("recorded_at") != producer.get("review_recorded_at")
                    or review_state.get("decision") != "pass"
                    or review_state.get("report") != producer.get("review_path")
                    or review_state.get("report_sha256") != producer.get("review_sha256")
                ):
                    errors.append("Sprint outcome evidence producer 未绑定当前 attempt Review 状态")
        review_relative = producer.get("review_path")
        review_path = Path(review_relative) if isinstance(review_relative, str) else Path()
        if (
            not isinstance(review_relative, str)
            or not review_relative
            or review_path.is_absolute()
            or ".." in review_path.parts
            or not (root / review_path).resolve().is_relative_to(root.resolve())
            or review_path != (root / review_path).resolve().relative_to(root.resolve())
            or not (root / review_path).is_file()
        ):
            errors.append("Sprint outcome evidence producer.review_path 无效或不存在")
        elif producer.get("review_sha256") != hashlib.sha256((root / review_path).read_bytes()).hexdigest():
            errors.append("Sprint outcome evidence 未绑定当前终态任务 Review")
        else:
            try:
                review_document = json.loads((root / review_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"Sprint outcome evidence producer Review 无法读取: {exc}")
            else:
                if not isinstance(review_document, dict) or review_document.get("outcome") != producer_attestation:
                    errors.append("Sprint outcome evidence 未被终态任务 Review 明确认可")
        if attempt_state is not None:
            review_state = attempt_state.get("review") or {}
            review_artifacts = review_state.get("artifacts") if isinstance(review_state, dict) else None
            if not isinstance(review_artifacts, list) or not all(isinstance(item, dict) for item in review_artifacts):
                errors.append("Sprint outcome evidence producer Review artifacts 格式非法")
            else:
                artifacts = {item.get("path"): item.get("sha256") for item in review_artifacts}
                if any(
                    not isinstance(item, dict) or artifacts.get(item.get("path")) != item.get("sha256")
                    for item in evidence or []
                ):
                    errors.append("Sprint outcome evidence 未绑定当前 Review 已记录的实际 artifact")
    elif producer.get("kind") == "boss-signoff":
        signoff_relative = producer.get("path")
        signoff_path = Path(signoff_relative) if isinstance(signoff_relative, str) else Path()
        if (
            not isinstance(signoff_relative, str)
            or not signoff_relative
            or signoff_path.is_absolute()
            or ".." in signoff_path.parts
            or not (root / signoff_path).resolve().is_relative_to(root.resolve())
            or signoff_path != (root / signoff_path).resolve().relative_to(root.resolve())
            or not (root / signoff_path).is_file()
        ):
            errors.append("Sprint outcome evidence producer Boss signoff 无效或不存在")
        else:
            try:
                loaded = yaml.safe_load((root / signoff_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                errors.append(f"Sprint outcome evidence producer Boss signoff 无法读取: {exc}")
                loaded = None
            if (
                not isinstance(loaded, dict)
                or loaded.get("decision") != "approved"
                or loaded.get("planning_contract_sha256") != planning_contract_digest(plan)
                or loaded.get("outcome") != producer_attestation
            ):
                errors.append("Sprint outcome evidence producer Boss signoff 未批准当前契约及实际结果证据")
    else:
        errors.append("Sprint outcome evidence.producer.kind 必须是 task-review 或 boss-signoff")
    expected = contract.get("outcome_acceptance") if isinstance(contract.get("outcome_acceptance"), list) else []
    if not isinstance(evidence, list):
        errors.append("Sprint outcome evidence.evidence 必须是数组")
        evidence = []
    covered: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or set(item) != {"acceptance", "path", "sha256", "observed", "result"}:
            errors.append(f"Sprint outcome evidence.evidence[{index}] 必须包含 acceptance/path/sha256/observed/result")
            continue
        acceptance = item.get("acceptance")
        relative = item.get("path")
        if acceptance not in expected:
            errors.append(f"Sprint outcome evidence 引用了未知验收条件: {acceptance}")
        else:
            covered.add(acceptance)
        normalized_observed = re.sub(r"\W+", "", str(item.get("observed", ""))).casefold()
        normalized_acceptance = re.sub(r"\W+", "", str(acceptance or "")).casefold()
        if (
            item.get("result") != "pass"
            or not _concrete_text(item.get("observed"))
            or normalized_observed == normalized_acceptance
        ):
            errors.append(f"Sprint outcome evidence 未记录可判定 observed/result: {acceptance}")
        evidence_path = Path(relative) if isinstance(relative, str) else Path()
        if (
            not isinstance(relative, str)
            or not relative
            or evidence_path.is_absolute()
            or ".." in evidence_path.parts
            or not (root / evidence_path).resolve().is_relative_to(root.resolve())
            or evidence_path != (root / evidence_path).resolve().relative_to(root.resolve())
            or not (root / evidence_path).is_file()
        ):
            errors.append(f"Sprint outcome evidence 路径无效或不存在: {relative}")
        elif isinstance(acceptance, str):
            try:
                evidence_bytes = (root / evidence_path).read_bytes()
                evidence_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                errors.append(f"Sprint outcome evidence 必须引用可读取的文本证据: {relative}")
            else:
                if item.get("sha256") != hashlib.sha256(evidence_bytes).hexdigest():
                    errors.append(f"Sprint outcome evidence 未绑定当前证据内容: {relative}")
    missing = set(expected) - covered
    if missing:
        errors.append(f"Sprint outcome evidence 未覆盖验收条件: {sorted(missing)}")
    return errors


def _row_value(row: dict[str, str], *names: str) -> str:
    return next((str(row.get(name, "")).strip() for name in names if row.get(name)), "")


def validate_sprint_planning_contract(
    path: Path,
    rows: list[dict[str, str]],
    planning: dict[str, Any],
    architecture_path: Path | None = None,
    delivery_strategies: dict[str, Any] | None = None,
    sprint_delivery_contracts: dict[str, Any] | None = None,
    user_stories_path: Path | None = None,
    impact_surface_requirements: dict[str, Any] | None = None,
    sprint_requirement_modes: dict[str, Any] | None = None,
) -> list[str]:
    """Validate Sprint-level breadth and externally observable delivery intent."""
    contract = sprint_planning_contract(path)
    errors: list[str] = []
    if ambiguous_fields := duplicate_sprint_contract_fields(path):
        errors.append(f"Sprint 合同字段必须且只能声明一次: {ambiguous_fields}")
    contract_version = contract["planning_contract_version"]
    if contract_version not in {1, 2, 3}:
        errors.append("planning_contract_version 必须为 1（兼容）、2（兼容）或 3")
    transfer_source = header_field(path.read_text(encoding="utf-8"), "scope_transfer_from")
    if transfer_source:
        parts = transfer_source.split("/")
        if (
            contract_version != 3
            or len(parts) != 3
            or not SPRINT_ID.fullmatch(parts[0])
            or not TASK_ID.fullmatch(parts[1])
            or not re.fullmatch(r"[0-9a-f]{32}", parts[2])
        ):
            errors.append("scope_transfer_from 必须使用 v3 合同并符合 <Sprint ID>/<Task ID>/<32位Run ID>")
    strategies = delivery_strategies or {}
    sprint_contract = (sprint_delivery_contracts or {}).get(sprint_header(path)["sprint_type"], {})
    strategy = contract["delivery_strategy"]
    if not isinstance(strategy, str) or strategy not in strategies:
        errors.append(f"delivery_strategy 必须是 {sorted(strategies)} 之一")
    outcomes = contract["observable_outcomes"]
    if not isinstance(outcomes, list) or not outcomes or not all(isinstance(item, str) for item in outcomes):
        errors.append("observable_outcomes 必须是非空字符串数组")
    known_outcomes = {
        outcome
        for definition in strategies.values()
        if isinstance(definition, dict)
        for outcome in definition.get("observable_outcomes", [])
        if isinstance(outcome, str)
    }
    if (
        isinstance(outcomes, list)
        and all(isinstance(item, str) for item in outcomes)
        and (unknown := set(outcomes) - known_outcomes)
    ):
        errors.append(f"observable_outcomes 包含未知类型: {sorted(unknown)}")
    observation = contract["boss_observation"]
    if (
        not isinstance(observation, dict)
        or set(observation) != {"entrypoint", "action"}
        or not all(_concrete_text(observation.get(field)) for field in ("entrypoint", "action"))
    ):
        errors.append("boss_observation 必须是包含具体 entrypoint 和 action 的对象")
    acceptance = contract["outcome_acceptance"]
    if not isinstance(acceptance, list) or not acceptance or not all(_concrete_text(item) for item in acceptance):
        errors.append("outcome_acceptance 必须是非空、可判定的预期结果数组")

    sprint_type = sprint_header(path)["sprint_type"]
    uses_stories = sprint_uses_story_requirements(sprint_type, contract)
    if contract_version == 3:
        configured_modes = sprint_requirement_modes or {}
        allowed_modes = set(configured_modes.get(sprint_type) or [])
        requirement_mode = contract.get("requirement_mode")
        if requirement_mode not in allowed_modes:
            errors.append(f"sprint_type={sprint_type} requirement_mode 必须是 {sorted(allowed_modes)} 之一")
        if requirement_mode == "non-product-change" and contract.get("source_stories") not in ([], None):
            errors.append("requirement_mode=non-product-change 时 source_stories 必须为空数组")
    if uses_stories:
        if user_stories_path is None:
            errors.append("requirement_mode=stories 必须提供 USER_STORIES.md 输入")
        else:
            _, story_errors = validate_source_stories(user_stories_path, contract["source_stories"])
            errors.extend(story_errors)
    if contract_version in {2, 3} and sprint_type == "feature-sprint":
        impacts = contract["impact_surfaces"]
        if not isinstance(impacts, dict) or not impacts:
            errors.append("impact_surfaces 必须是影响面到具体理由的非空对象")
        elif not all(isinstance(name, str) and _concrete_text(reason) for name, reason in impacts.items()):
            errors.append("impact_surfaces 每项必须包含已知影响面和不少于 8 字的具体理由")
        requirements = impact_surface_requirements or {}
        if isinstance(impacts, dict):
            if unknown_impacts := set(impacts) - set(requirements):
                errors.append(f"impact_surfaces 包含未知影响面: {sorted(unknown_impacts)}")
            required_tasks: set[str] = set()
            required_facets: dict[str, set[str]] = {}
            for surface in set(impacts) & set(requirements):
                definition = requirements.get(surface) or {}
                required_tasks.update(definition.get("tasks") or [])
                for task_name, facets in (definition.get("facets") or {}).items():
                    required_facets.setdefault(task_name, set()).update(facets)
            planned_design = {
                _row_value(row, "类型", "type")
                for row in rows
                if _row_value(row, "类型", "type") in {"design", "backend-design", "frontend-design"}
            }
            if planned_design != required_tasks:
                errors.append(
                    "影响面要求的设计任务与 Sprint 不一致: "
                    f"required={sorted(required_tasks)}, planned={sorted(planned_design)}"
                )
            duplicate_design = sorted(
                task_name
                for task_name in {"design", "backend-design", "frontend-design"}
                if sum(_row_value(row, "类型", "type") == task_name for row in rows) > 1
            )
            if duplicate_design:
                errors.append(f"每种设计任务最多只能声明一次，以唯一绑定 facets: {duplicate_design}")
            for task_name in required_tasks:
                task_rows = [row for row in rows if _row_value(row, "类型", "type") == task_name]
                if len(task_rows) != 1:
                    errors.append(f"影响面要求设计任务 {task_name} 恰好声明一次，实际 {len(task_rows)} 次")
                    continue
                raw_facets = _row_value(task_rows[0], "facets", "任务属性", "能力面")
                selected_facets = {item.strip() for item in raw_facets.replace("，", ",").split(",") if item.strip()}
                expected_facets = required_facets.get(task_name, set())
                if selected_facets != expected_facets:
                    errors.append(
                        f"{task_name} facets 与影响面不一致: "
                        f"required={sorted(expected_facets)}, planned={sorted(selected_facets)}"
                    )
    allowed_strategies = set(sprint_contract.get("strategies", [])) if isinstance(sprint_contract, dict) else set()
    if isinstance(strategy, str) and strategy not in allowed_strategies:
        errors.append(f"sprint_type={sprint_type} 只允许 delivery_strategy: {sorted(allowed_strategies)}")
    if isinstance(strategy, str) and isinstance(outcomes, list) and all(isinstance(item, str) for item in outcomes):
        definition = strategies.get(strategy, {})
        compatible = set(definition.get("observable_outcomes", [])) if isinstance(definition, dict) else set()
        if not set(outcomes) & compatible:
            errors.append(f"delivery_strategy={strategy} 必须包含匹配的 observable_outcomes: {sorted(compatible)}")
        required_outcomes = set(definition.get("required_outcomes", [])) if isinstance(definition, dict) else set()
        if missing_outcomes := required_outcomes - set(outcomes):
            errors.append(f"delivery_strategy={strategy} 缺少 required_outcomes: {sorted(missing_outcomes)}")
    terminal_tasks = set(sprint_contract.get("terminal_tasks", [])) if isinstance(sprint_contract, dict) else set()
    planned_types = {_row_value(row, "类型", "type") for row in rows}
    if terminal_tasks and not planned_types & terminal_tasks:
        errors.append(f"Sprint 计划必须包含至少一个 outcome 终态任务: {sorted(terminal_tasks)}")

    project_stage = planning.get("project_stage")
    minimum_domains = planning.get("minimum_bootstrap_domains")
    bootstrap_required = sprint_type in BOOTSTRAP_SPRINT_TYPES and project_stage == "greenfield"
    domains = contract["domain_coverage"]
    if architecture_path is not None:
        errors.extend(
            validate_bootstrap_completion(
                architecture_path.parent,
                planning,
                architecture_path,
                delivery_strategies=delivery_strategies,
                sprint_delivery_contracts=sprint_delivery_contracts,
                sprint_requirement_modes=sprint_requirement_modes,
            )
        )
    design_types = {"backend-design", "frontend-design", "design"}
    if bootstrap_required:
        if strategy != "domain-bootstrap":
            errors.append(f"project_stage=greenfield 的 {sprint_type} 必须持续使用 delivery_strategy: domain-bootstrap")
        declared_bootstrap_domains = architecture_domains(architecture_path) if architecture_path else set()
        if not declared_bootstrap_domains:
            errors.append("project_stage=greenfield 必须先在 ARCHITECTURE.md 声明至少一个可解析领域")
        required_count = min(minimum_domains, len(declared_bootstrap_domains)) if type(minimum_domains) is int else 0
        if not isinstance(domains, dict) or len(domains) < required_count:
            errors.append(f"domain-bootstrap 必须映射 ARCHITECTURE.md 声明的全部领域（当前 {required_count} 个）")
        elif {str(item).casefold() for item in domains} != declared_bootstrap_domains:
            errors.append("domain-bootstrap 的 domain_coverage 必须完整且仅覆盖 ARCHITECTURE.md 全部领域")
    if isinstance(domains, dict) and domains:
        declared_domains = architecture_domains(architecture_path) if architecture_path else set()
        if not declared_domains:
            errors.append("domain_coverage 非空时 ARCHITECTURE.md 必须声明可解析的领域清单")
        else:
            unknown_domains = {str(item).casefold() for item in domains} - declared_domains
            if unknown_domains:
                errors.append(f"domain_coverage 包含 ARCHITECTURE.md 未声明的领域: {sorted(unknown_domains)}")
        row_by_id = {_row_value(row, "id"): row for row in rows}
        for domain, task_ids in domains.items():
            if not isinstance(task_ids, list) or not task_ids or not all(isinstance(item, str) for item in task_ids):
                errors.append(f"domain_coverage.{domain} 必须是非空任务 ID 数组")
                continue
            unknown_tasks = set(task_ids) - set(row_by_id)
            if unknown_tasks:
                errors.append(f"domain_coverage.{domain} 引用了未知任务: {sorted(unknown_tasks)}")
                continue
            mapped_types = {_row_value(row_by_id[item], "类型", "type") for item in task_ids}
            domain_required = {"library-design", "library-code"} if sprint_type == "library-sprint" else {"code"}
            if sprint_type == "feature-sprint" and not mapped_types & design_types:
                errors.append(f"domain_coverage.{domain} 缺少领域设计任务")
            if not domain_required <= mapped_types:
                errors.append(f"domain_coverage.{domain} 缺少实现任务: {sorted(domain_required - mapped_types)}")
            for task_id in task_ids:
                row = row_by_id[task_id]
                if not _concrete_text(_row_value(row, "产出物", "outputs")):
                    errors.append(f"映射任务 {task_id} 必须声明具体产出物")
                if not _concrete_text(_row_value(row, "验收条件", "acceptance")):
                    errors.append(f"映射任务 {task_id} 必须声明具体验收条件")
    if bootstrap_required:
        task_types = {_row_value(row, "类型", "type") for row in rows}
        required = {"library-design", "library-code"} if sprint_type == "library-sprint" else {"infra", "code"}
        missing = required - task_types
        if sprint_type == "feature-sprint" and not task_types & design_types:
            missing.add("design/backend-design/frontend-design")
        if missing:
            errors.append(f"domain-bootstrap 缺少领域框架设计/实现任务: {sorted(missing)}")
    elif domains is not None and (
        not isinstance(domains, dict)
        or not all(isinstance(key, str) and key.strip() and isinstance(value, list) for key, value in domains.items())
    ):
        errors.append("domain_coverage 必须是领域到任务 ID 数组的对象")
    return errors


def table_rows(content: str) -> list[dict[str, str]]:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        headers = [cell.strip().lower() for cell in line.strip().strip("|").split("|")]
        if "id" not in headers or not ({"类型", "type"} & set(headers)):
            continue
        if index + 1 >= len(lines):
            return []
        rows: list[dict[str, str]] = []
        for value in lines[index + 2 :]:
            if not value.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells, strict=True)))
        return rows
    return []


def task_dependency_graph(rows: list[dict[str, str]]) -> tuple[dict[str, set[str]], list[str]]:
    """Parse explicit task-ID dependencies from the Sprint table."""
    task_ids = {_row_value(row, "id") for row in rows if _row_value(row, "id")}
    graph: dict[str, set[str]] = {}
    errors: list[str] = []
    empty = {"", "-", "—", "无", "none", "[]"}
    for row in rows:
        task_id = _row_value(row, "id")
        if not task_id:
            continue
        if not any(name in row for name in ("依赖", "dependencies", "dependency")):
            errors.append(f"Sprint 任务 {task_id} 缺少依赖列")
            graph[task_id] = set()
            continue
        raw = _row_value(row, "依赖", "dependencies", "dependency")
        values = [] if raw.casefold() in empty else [item.strip() for item in re.split(r"[,，;；]", raw)]
        dependencies = {value for value in values if value}
        if len(dependencies) != len([value for value in values if value]):
            errors.append(f"Sprint 任务 {task_id} 依赖 ID 不得重复")
        invalid = sorted(value for value in dependencies if not TASK_ID.fullmatch(value))
        if invalid:
            errors.append(f"Sprint 任务 {task_id} 包含非法依赖 ID: {invalid}")
        if task_id in dependencies:
            errors.append(f"Sprint 任务 {task_id} 不得依赖自己")
        if unknown := sorted(dependencies - task_ids):
            errors.append(f"Sprint 任务 {task_id} 依赖未登记任务: {unknown}")
        graph[task_id] = dependencies & (task_ids - {task_id})
    return graph, errors


def transitive_task_dependencies(graph: dict[str, set[str]], task_id: str) -> set[str]:
    """Return the dependency closure for one task; validation owns cycle rejection."""
    result: set[str] = set()
    pending = list(graph.get(task_id, set()))
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(graph.get(dependency, set()) - result)
    return result


def validate_task_dependencies(rows: list[dict[str, str]], stages: list[Any]) -> list[str]:
    """Validate an acyclic, forward stage chain expressed with concrete task IDs."""
    graph, errors = task_dependency_graph(rows)
    stage_types = [
        {
            str(value)
            for value in (stage.get("tasks", []) if isinstance(stage, dict) else stage)
            if isinstance(value, str)
        }
        for stage in stages
    ]
    type_stage = {task_type: index for index, values in enumerate(stage_types) for task_type in values}
    row_by_id = {_row_value(row, "id"): row for row in rows if _row_value(row, "id")}
    stage_by_id = {task_id: type_stage.get(_row_value(row, "类型", "type"), -1) for task_id, row in row_by_id.items()}
    for task_id, dependencies in graph.items():
        current_stage = stage_by_id.get(task_id, -1)
        reverse = sorted(
            dependency for dependency in dependencies if stage_by_id.get(dependency, -1) > current_stage >= 0
        )
        if reverse:
            errors.append(f"Sprint 任务 {task_id} 不得依赖后续阶段任务: {reverse}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"Sprint 任务依赖存在循环: {task_id}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph.get(task_id, set()):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)

    for task_id, current_stage in stage_by_id.items():
        if current_stage <= 0:
            continue
        earlier_stages = {
            candidate_stage for candidate_stage in stage_by_id.values() if 0 <= candidate_stage < current_stage
        }
        if not earlier_stages:
            continue
        nearest_stage = max(earlier_stages)
        earlier_tasks = {
            candidate for candidate, candidate_stage in stage_by_id.items() if candidate_stage == nearest_stage
        }
        reached = transitive_task_dependencies(graph, task_id)
        if not reached.intersection(earlier_tasks):
            errors.append(f"Sprint 任务 {task_id} 必须显式依赖最近的实际前序阶段任务: {sorted(earlier_tasks)}")
    return list(dict.fromkeys(errors))


def sprint_structure_digest_bytes(value: bytes) -> str:
    """Hash immutable lifecycle fields and task structure, excluding prose/status."""
    content = value.decode("utf-8")
    rows = []
    for row in table_rows(content):
        rows.append({key: value for key, value in row.items() if key not in {"status", "状态"}})
    header = {field: header_field(content, field) for field in SPRINT_HEADER_FIELDS}
    structured: dict[str, Any] = {"header": header, "tasks": rows}
    planning_contract = canonical_planning_contract_from_content(content)
    if planning_contract["planning_contract_version"] is not None:
        structured["planning_contract"] = planning_contract
    payload = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def sprint_structure_digest(path: Path) -> str:
    return sprint_structure_digest_bytes(path.read_bytes())


def sprint_policy(rules: dict[str, Any], sprint_type: str) -> dict[str, str]:
    policy = (rules.get("sprint_type_policies") or {}).get(sprint_type)
    if not isinstance(policy, dict):
        raise ValueError(f"Sprint 类型缺少生命周期策略: {sprint_type}")
    return {key: str(value) for key, value in policy.items()}


def branch_name(sprint_id: str, policy: dict[str, str]) -> str:
    prefix = policy.get("branch_prefix", "")
    suffix = sprint_id.removeprefix("sprint-")
    return f"{prefix}{suffix}" if prefix else ""

"""Machine-validated scope registries for product, design, and technical documents."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_TASKS = {
    "product": ("product-specs", "product"),
    "design": ("design-docs", "design"),
    "backend-design": ("tech-docs", "backend"),
    "frontend-design": ("tech-docs", "frontend"),
    "library-design": ("tech-docs", "library"),
}
REGISTRY_DIRECTORIES = frozenset(value[0] for value in REGISTRY_TASKS.values())
REGISTRY_COLUMNS = {
    "product-specs": (
        "Entry ID",
        "Scope Key",
        "文件",
        "SHA-256",
        "模块",
        "页面/功能区域",
        "关联 Story/AC",
        "Sprint",
        "Task ID",
        "Run ID",
        "状态",
        "Supersedes",
    ),
    "design-docs": (
        "Entry ID",
        "Scope Key",
        "文件",
        "SHA-256",
        "模块",
        "页面/功能区域",
        "关联 Story/AC",
        "Sprint",
        "Task ID",
        "Run ID",
        "状态",
        "Supersedes",
    ),
    "tech-docs": (
        "Entry ID",
        "Scope Key",
        "文件",
        "SHA-256",
        "方案类型",
        "模块",
        "表/API",
        "组件",
        "关联 Story/AC",
        "Sprint",
        "Task ID",
        "Run ID",
        "状态",
        "Supersedes",
    ),
}
VALID_STATUS = {"verified", "stale", "draft"}
VALID_TECH_KIND = {"backend", "frontend", "library"}
TECHNICAL_DESIGN_CORE_SECTIONS = {
    "backend": (
        "来源与约束回读",
        "现状与最小变更",
        "建模与核心流程",
        "契约与验证",
        "风险、回退与未决项",
    ),
    "frontend": (
        "来源与约束回读",
        "现状与最小变更",
        "组件与状态所有权",
        "契约与验证",
        "风险、回退与未决项",
    ),
}
TECHNICAL_DESIGN_CONTRACT_VERSION = 1
TECHNICAL_DESIGN_CONTRACT_MARKER = re.compile(
    rf"(?m)^technical_design_contract_version:\s*{TECHNICAL_DESIGN_CONTRACT_VERSION}\s*$"
)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,127}$")
SPRINT_ID = re.compile(r"^sprint-[A-Za-z0-9][A-Za-z0-9._-]*$")
SOURCE_REFERENCE = re.compile(r"(?:US-[0-9]+(?:-AC-[0-9]+)?|ARCHITECTURE|ASSIGNMENT)", re.I)
STORY_REFERENCE = re.compile(r"US-[0-9]+(?:-AC-[0-9]+)?")
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
EMPTY = {"", "—", "-", "_(待生成)_"}


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class RegistryRow:
    entry_id: str
    scope_key: str
    file: str
    file_sha256: str
    module: str
    story: str
    sprint: str
    task_id: str
    run_id: str
    status: str
    supersedes: tuple[str, ...]
    raw: dict[str, str]
    line: int


def registry_markdown_table(content: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|") or index + 1 >= len(lines):
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        separator = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
        if len(headers) != len(separator) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            continue
        rows: list[tuple[int, dict[str, str]]] = []
        for line_number, raw in enumerate(lines[index + 2 :], index + 3):
            if not raw.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append((line_number, dict(zip(headers, cells, strict=True))))
        return headers, rows
    return [], []


def registry_link_target(value: str) -> str:
    match = re.fullmatch(r"\[[^]]+]\(([^)#]+)(?:#[^)]+)?\)", value.strip())
    return match.group(1).strip() if match else value.strip().strip("`")


def _section_table(content: str, title: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    sections = list(
        re.finditer(
            rf"(?ms)^#{{2,4}}\s+{re.escape(title)}\s*$\n?(.*?)(?=^#{{1,4}}\s+|\Z)",
            content,
        )
    )
    if len(sections) != 1:
        return [], []
    return registry_markdown_table(sections[0].group(1))


def _inventory_identity(
    directory_name: str,
    file: str,
    raw: dict[str, str],
) -> tuple[str, ...]:
    common = (
        file,
        raw.get("Scope Key", "").strip().strip("`"),
        raw.get("模块", "").strip(),
    )
    if directory_name in {"product-specs", "design-docs"}:
        return (*common, raw.get("页面/功能区域", "").strip())
    return (
        *common,
        raw.get("表/API", "").strip(),
        raw.get("组件", "").strip(),
    )


def _document_inventory(
    document: Path,
    directory_name: str,
    relative_file: str,
) -> tuple[set[tuple[str, ...]], list[str]]:
    try:
        content = document.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return set(), [f"作用域清单无法读取: {document}: {exc}"]
    title = "最小范围与追溯矩阵" if directory_name == "product-specs" else "作用域清单"
    headers, rows = _section_table(content, title)
    required = {"Scope Key", "模块"}
    if directory_name in {"product-specs", "design-docs"}:
        required.add("页面/功能区域")
    else:
        required.update({"表/API", "组件"})
    if not rows or not required <= set(headers):
        return set(), [f"{document} 的『{title}』必须包含且填写列: {sorted(required)}"]
    inventory: set[tuple[str, ...]] = set()
    errors: list[str] = []
    for line, raw in rows:
        identity = _inventory_identity(directory_name, relative_file, raw)
        scope_key, module = identity[1:3]
        if not IDENTIFIER.fullmatch(scope_key):
            errors.append(f"{document}:{line}: Scope Key 非法: {scope_key or '空'}")
        if module in EMPTY:
            errors.append(f"{document}:{line}: 模块不能为空")
        if directory_name in {"product-specs", "design-docs"} and identity[3] in EMPTY:
            errors.append(f"{document}:{line}: 页面/功能区域不能为空")
        if directory_name == "tech-docs" and identity[3] in EMPTY and identity[4] in EMPTY:
            errors.append(f"{document}:{line}: 表/API 与组件不能同时为空")
        if identity in inventory:
            errors.append(f"{document}:{line}: 作用域清单行重复: {identity[1:]}")
        inventory.add(identity)
    return inventory, errors


def validate_technical_design_structure(document: Path, tech_kind: str) -> list[str]:
    """Require the small, stable core structure declared by backend/frontend design specs."""
    required = TECHNICAL_DESIGN_CORE_SECTIONS.get(tech_kind)
    if required is None:
        return []
    try:
        content = document.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"技术方案无法读取: {document}: {exc}"]
    errors: list[str] = []
    if len(TECHNICAL_DESIGN_CONTRACT_MARKER.findall(content)) != 1:
        errors.append(
            f"{document}: 新版技术方案必须且只能声明一次 "
            f"technical_design_contract_version: {TECHNICAL_DESIGN_CONTRACT_VERSION}"
        )
    for heading in required:
        matches = list(re.finditer(rf"(?m)^##\s+{re.escape(heading)}\s*$", content))
        if len(matches) != 1:
            errors.append(f"{document}: 技术方案必须且只能包含一个二级章节『{heading}』")
            continue
        start = matches[0].end()
        next_heading = re.search(r"(?m)^##\s+", content[start:])
        end = start + next_heading.start() if next_heading else len(content)
        body = re.sub(r"[\s#>*_`|:-]", "", content[start:end])
        if len(body) < 8 or re.fullmatch(r"(?i)(?:TODO|TBD|待补|待定|占位)+", body):
            errors.append(f"{document}: 技术方案核心章节『{heading}』不能为空或使用占位内容")
    return errors


def _references(value: str) -> tuple[str, ...]:
    if value.strip() in EMPTY:
        return ()
    return tuple(item.strip().strip("`") for item in re.split(r"[,，;；]", value) if item.strip())


def _parse_rows(index: Path, directory_name: str) -> tuple[list[RegistryRow], list[str]]:
    try:
        headers, raw_rows = registry_markdown_table(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"作用域索引无法读取: {index}: {exc}"]
    expected = REGISTRY_COLUMNS[directory_name]
    if tuple(headers) != expected:
        return [], [f"作用域索引列非法: {index}；必须精确为 {' | '.join(expected)}"]
    rows: list[RegistryRow] = []
    errors: list[str] = []
    for line, raw in raw_rows:
        if raw.get("Entry ID", "").strip() in EMPTY:
            continue
        row = RegistryRow(
            entry_id=raw["Entry ID"].strip().strip("`"),
            scope_key=raw["Scope Key"].strip().strip("`"),
            file=registry_link_target(raw["文件"]),
            file_sha256=raw["SHA-256"].strip().strip("`"),
            module=raw["模块"].strip(),
            story=raw["关联 Story/AC"].strip(),
            sprint=raw["Sprint"].strip().strip("`"),
            task_id=raw["Task ID"].strip().strip("`"),
            run_id=raw["Run ID"].strip().strip("`"),
            status=raw["状态"].strip(),
            supersedes=_references(raw["Supersedes"]),
            raw=raw,
            line=line,
        )
        prefix = f"{index}:{line}"
        if not IDENTIFIER.fullmatch(row.entry_id):
            errors.append(f"{prefix}: Entry ID 非法: {row.entry_id or '空'}")
        if not IDENTIFIER.fullmatch(row.scope_key):
            errors.append(f"{prefix}: Scope Key 非法: {row.scope_key or '空'}")
        relative = Path(row.file)
        if (
            not row.file
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.casefold() != ".md"
            or relative.name == "index.md"
        ):
            errors.append(f"{prefix}: 文件必须是当前目录内的 Markdown 产物: {row.file or '空'}")
        if not re.fullmatch(r"[0-9a-f]{64}", row.file_sha256):
            errors.append(f"{prefix}: SHA-256 必须绑定文档当前内容")
        if row.module in EMPTY:
            errors.append(f"{prefix}: 模块不能为空")
        if row.story in EMPTY or not SOURCE_REFERENCE.search(row.story):
            errors.append(f"{prefix}: 关联 Story/AC 必须引用 Story/AC、ARCHITECTURE 或 ASSIGNMENT")
        if not SPRINT_ID.fullmatch(row.sprint):
            errors.append(f"{prefix}: Sprint ID 非法: {row.sprint or '空'}")
        if not IDENTIFIER.fullmatch(row.task_id):
            errors.append(f"{prefix}: Task ID 非法: {row.task_id or '空'}")
        if not RUN_ID.fullmatch(row.run_id):
            errors.append(f"{prefix}: Run ID 必须是当前 attempt 的 32 位十六进制 ID")
        if row.status not in VALID_STATUS:
            errors.append(f"{prefix}: 状态必须是 verified/stale/draft")
        if directory_name in {"product-specs", "design-docs"} and raw["页面/功能区域"].strip() in EMPTY:
            errors.append(f"{prefix}: 页面/功能区域不能为空")
        if directory_name == "tech-docs":
            if raw["方案类型"].strip() not in VALID_TECH_KIND:
                errors.append(f"{prefix}: 方案类型必须是 backend/frontend/library")
            if raw["表/API"].strip() in EMPTY and raw["组件"].strip() in EMPTY:
                errors.append(f"{prefix}: 表/API 与组件不能同时为空")
        rows.append(row)
    return rows, errors


def validate_registry(root: Path, directory_name: str, *, docs_dir: Path | None = None) -> list[str]:
    """Validate one project-owned registry and its current/supersession graph."""
    if directory_name not in REGISTRY_COLUMNS:
        return [f"未知作用域索引目录: {directory_name}"]
    directory = (docs_dir or root / "docs") / directory_name
    if not directory.exists():
        return []
    index = directory / "index.md"
    if not index.is_file():
        return [f"缺少作用域索引: {index}"]
    rows, errors = _parse_rows(index, directory_name)
    by_id: dict[str, RegistryRow] = {}
    by_scope: dict[str, list[RegistryRow]] = {}
    file_sprints: dict[str, set[str]] = {}
    for row in rows:
        if row.entry_id in by_id:
            errors.append(f"{index}:{row.line}: Entry ID 重复: {row.entry_id}")
        else:
            by_id[row.entry_id] = row
        by_scope.setdefault(row.scope_key, []).append(row)
        file_sprints.setdefault(row.file, set()).add(row.sprint)
        target = (directory / row.file).resolve()
        if not target.is_relative_to(directory.resolve()) or not target.is_file():
            errors.append(f"{index}:{row.line}: 索引文件不存在: {row.file}")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != row.file_sha256:
            errors.append(f"{index}:{row.line}: 已登记文档内容已变化: {row.file}")
    for file, sprints in file_sprints.items():
        if len(sprints) > 1:
            errors.append(f"{index}: 历史产物被跨 Sprint 复用或覆盖: {file} ({', '.join(sorted(sprints))})")
    for scope, scope_rows in by_scope.items():
        verified = [row for row in scope_rows if row.status == "verified"]
        if len(verified) > 1:
            errors.append(f"{index}: Scope Key 存在多个 current(verified) 条目: {scope}")
        if not verified and any(row.status != "draft" for row in scope_rows):
            errors.append(f"{index}: Scope Key 的历史条目存在时必须保留一个 current(verified): {scope}")
        for row in scope_rows:
            for target_id in row.supersedes:
                target = by_id.get(target_id)
                if target is None:
                    errors.append(f"{index}:{row.line}: Supersedes 引用了未知 Entry ID: {target_id}")
                elif target.entry_id == row.entry_id:
                    errors.append(f"{index}:{row.line}: 条目不能 Supersedes 自己")
                elif target.scope_key != row.scope_key:
                    errors.append(f"{index}:{row.line}: Supersedes 目标不属于同一 Scope Key: {target_id}")
                elif target.status == "draft" or (row.status != "draft" and target.status != "stale"):
                    errors.append(
                        f"{index}:{row.line}: draft 只能覆盖 verified/stale，已发布条目只能覆盖 stale: {target_id}"
                    )
        if len(scope_rows) > 1 and verified:
            current = verified[0]
            stale_ids = {row.entry_id for row in scope_rows if row.status == "stale"}
            if stale_ids and not stale_ids.intersection(current.supersedes):
                errors.append(f"{index}:{current.line}: current 条目必须 Supersedes 同 Scope Key 的上一条 stale 产物")
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(entry_id: str) -> None:
        if entry_id in visiting:
            errors.append(f"{index}: Supersedes 存在循环: {entry_id}")
            return
        if entry_id in visited or entry_id not in by_id:
            return
        visiting.add(entry_id)
        for target in by_id[entry_id].supersedes:
            visit(target)
        visiting.remove(entry_id)
        visited.add(entry_id)

    for entry_id in by_id:
        visit(entry_id)
    indexed = {row.file for row in rows}
    for document in sorted(directory.glob("*.md")):
        if document.name != "index.md" and document.name not in indexed:
            errors.append(f"未登记作用域: {document} 不在 {index} 中")
    inventory_title = "最小范围与追溯矩阵" if directory_name == "product-specs" else "作用域清单"
    for relative_file in sorted(indexed):
        document = directory / relative_file
        if not document.is_file():
            continue
        try:
            content = document.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_rows = [row for row in rows if row.file == relative_file]
        has_inventory = bool(re.search(rf"(?m)^#{{1,6}}\s+{re.escape(inventory_title)}\s*$", content))
        has_structure_contract = directory_name == "tech-docs" and bool(
            TECHNICAL_DESIGN_CONTRACT_MARKER.search(content)
        )
        requires_technical_contract = directory_name == "tech-docs" and (
            any(row.status == "draft" for row in file_rows) or has_structure_contract
        )
        if not has_inventory and not requires_technical_contract:
            continue
        inventory, inventory_errors = _document_inventory(document, directory_name, relative_file)
        errors.extend(inventory_errors)
        registered = {
            _inventory_identity(directory_name, row.file, row.raw) for row in rows if row.file == relative_file
        }
        if missing := sorted(inventory - registered):
            errors.append(f"{document}: 正文作用域清单存在未登记行: {missing}")
        if extra := sorted(registered - inventory):
            errors.append(f"{document}: 注册表存在正文未声明行: {extra}")
        if requires_technical_contract:
            tech_kinds = {row.raw["方案类型"].strip() for row in file_rows}
            for tech_kind in sorted(tech_kinds):
                errors.extend(validate_technical_design_structure(document, tech_kind))
    return errors


def validate_unpublished_task_registry(
    root: Path,
    sprint_id: str,
    task_id: str,
    run_id: str,
    task_type: str,
) -> list[str]:
    """Reject an unpassed attempt that has already claimed published registry state."""
    registration = REGISTRY_TASKS.get(task_type)
    if registration is None:
        return []
    directory_name, expected_kind = registration
    errors = validate_registry(root, directory_name)
    index = root / "docs" / directory_name / "index.md"
    if not index.is_file():
        return errors
    rows, parse_errors = _parse_rows(index, directory_name)
    if parse_errors:
        return errors
    for row in rows:
        if row.sprint != sprint_id or row.task_id != task_id or row.run_id != run_id:
            continue
        if row.status != "draft":
            errors.append(f"未通过 Review 的当前 attempt 只能保留 draft 条目: {row.entry_id}")
        if directory_name == "tech-docs" and row.raw["方案类型"].strip() != expected_kind:
            errors.append(f"技术方案类型与任务不一致: {row.entry_id} != {expected_kind}")
    return errors


def validate_task_registry(
    root: Path,
    sprint_id: str,
    task_id: str,
    run_id: str,
    task_type: str,
    artifact_paths: list[Path],
    source_stories: Any = None,
    requirement_mode: str | None = None,
) -> list[str]:
    """Require a passing design attempt to own draft rows for exactly its outputs."""
    registration = REGISTRY_TASKS.get(task_type)
    if registration is None:
        return []
    directory_name, expected_kind = registration
    errors = validate_registry(root, directory_name)
    index = root / "docs" / directory_name / "index.md"
    if not index.is_file():
        return errors
    rows, parse_errors = _parse_rows(index, directory_name)
    if parse_errors:
        return errors
    relative_root = Path("docs") / directory_name
    documents = [
        path.relative_to(relative_root).as_posix()
        for path in artifact_paths
        if path != relative_root / "index.md" and path.is_relative_to(relative_root) and path.suffix.casefold() == ".md"
    ]
    if not documents:
        errors.append(f"{task_type} Review 必须绑定至少一个已登记的 Markdown 方案产物")
        return errors
    task_rows = [
        row
        for row in rows
        if row.sprint == sprint_id
        and row.task_id == task_id
        and row.run_id == run_id
        and (directory_name != "tech-docs" or row.raw["方案类型"].strip() == expected_kind)
    ]
    expected_documents = {row.file for row in task_rows}
    if not task_rows:
        errors.append(f"作用域索引没有登记当前 attempt 产物: Sprint={sprint_id}, Task={task_id}, Run={run_id}")
    for row in task_rows:
        if row.status != "draft":
            errors.append(f"Exec 只能提交 draft 作用域条目，verified 由 Review PASS 发布: {row.entry_id}")
        current = [
            candidate
            for candidate in rows
            if candidate.scope_key == row.scope_key
            and candidate.status == "verified"
            and candidate.entry_id != row.entry_id
        ]
        if current and not {item.entry_id for item in current} <= set(row.supersedes):
            errors.append(f"draft 条目必须 Supersedes 当前 verified 条目: {row.entry_id}")
    for missing in sorted(expected_documents - set(documents)):
        errors.append(f"Review artifact 未绑定本轮索引产物: {relative_root / missing}")
    for document in sorted(set(documents)):
        matching = [row for row in rows if row.file == document]
        if not matching:
            errors.append(f"本轮产物未登记作用域: {relative_root / document}")
            continue
        for row in matching:
            if row.sprint != sprint_id or row.task_id != task_id or row.run_id != run_id or row.status != "draft":
                errors.append(
                    f"本轮产物必须全部登记为 Sprint={sprint_id}、Task={task_id}、Run={run_id}、状态=draft: "
                    f"{relative_root / document} ({row.entry_id})"
                )
            if directory_name == "tech-docs" and row.raw["方案类型"].strip() != expected_kind:
                errors.append(f"技术方案类型与任务不一致: {row.entry_id} != {expected_kind}")
    expected_sources = {
        identifier
        for reference in source_stories or []
        if isinstance(reference, dict)
        for identifier in [reference.get("id"), *(reference.get("acs") or [])]
        if isinstance(identifier, str)
    }
    for row in task_rows:
        story_sources = set(STORY_REFERENCE.findall(row.story))
        if requirement_mode == "stories":
            if not story_sources:
                errors.append(f"Story-bound 作用域条目必须关联本 Sprint 选中的 Story/AC: {row.entry_id}")
            if unknown := sorted(story_sources - expected_sources):
                errors.append(f"作用域条目引用了本 Sprint 未选择的 Story/AC: {row.entry_id}: {unknown}")
        elif requirement_mode == "non-product-change":
            if story_sources:
                errors.append(f"non-product-change 作用域条目不得引用 Story/AC: {row.entry_id}")
            if not re.search(r"ARCHITECTURE|ASSIGNMENT", row.story, re.I):
                errors.append(f"non-product-change 作用域条目必须关联 ARCHITECTURE 或 ASSIGNMENT: {row.entry_id}")
    inventory: set[tuple[str, ...]] = set()
    for document in sorted(set(documents)):
        values, inventory_errors = _document_inventory(
            root / relative_root / document,
            directory_name,
            document,
        )
        inventory.update(values)
        errors.extend(inventory_errors)
    registered = {_inventory_identity(directory_name, row.file, row.raw) for row in task_rows}
    if missing := sorted(inventory - registered):
        errors.append(f"正文作用域清单存在未登记行: {missing}")
    if extra := sorted(registered - inventory):
        errors.append(f"当前 attempt 注册表存在正文未声明行: {extra}")
    return errors


def promote_task_registry(
    root: Path,
    sprint_id: str,
    task_id: str,
    run_id: str,
    task_type: str,
) -> Path | None:
    """Publish only the current reviewed attempt and stale the entries it supersedes."""
    registration = REGISTRY_TASKS.get(task_type)
    if registration is None:
        return None
    directory_name, expected_kind = registration
    directory = root / "docs" / directory_name
    index = directory / "index.md"
    rows, errors = _parse_rows(index, directory_name)
    if errors:
        raise ValueError("作用域索引无法发布:\n- " + "\n- ".join(errors))
    current_rows = [
        row
        for row in rows
        if row.sprint == sprint_id
        and row.task_id == task_id
        and row.run_id == run_id
        and row.status == "draft"
        and (directory_name != "tech-docs" or row.raw["方案类型"].strip() == expected_kind)
    ]
    if not current_rows:
        raise ValueError("当前 Review attempt 没有可发布的 draft 作用域条目")
    publish_ids = {row.entry_id for row in current_rows}
    stale_ids = {entry_id for row in current_rows for entry_id in row.supersedes}
    original = index.read_text(encoding="utf-8")
    lines = original.splitlines()
    status_index = REGISTRY_COLUMNS[directory_name].index("状态")
    for row in rows:
        next_status = "verified" if row.entry_id in publish_ids else "stale" if row.entry_id in stale_ids else None
        if next_status is None:
            continue
        cells = [cell.strip() for cell in lines[row.line - 1].strip().strip("|").split("|")]
        cells[status_index] = next_status
        lines[row.line - 1] = "| " + " | ".join(cells) + " |"
    _atomic_write(index, "\n".join(lines).rstrip() + "\n")
    if publish_errors := validate_registry(root, directory_name):
        _atomic_write(index, original)
        raise ValueError("作用域索引发布后无效:\n- " + "\n- ".join(publish_errors))
    return index


def task_registry_publication(
    root: Path,
    sprint_id: str,
    task_id: str,
    run_id: str,
    task_type: str,
) -> dict[str, Any]:
    """Build the immutable per-attempt snapshot consumed by downstream tasks."""
    registration = REGISTRY_TASKS.get(task_type)
    if registration is None:
        return {}
    directory_name, expected_kind = registration
    index = root / "docs" / directory_name / "index.md"
    rows, errors = _parse_rows(index, directory_name)
    if errors:
        raise ValueError("作用域索引发布快照无效:\n- " + "\n- ".join(errors))
    published = [
        row
        for row in rows
        if row.sprint == sprint_id
        and row.task_id == task_id
        and row.run_id == run_id
        and row.status == "verified"
        and (directory_name != "tech-docs" or row.raw["方案类型"].strip() == expected_kind)
    ]
    if not published:
        raise ValueError("当前 attempt 没有已发布作用域条目")
    return {
        "schema_version": 1,
        "sprint": sprint_id,
        "task_id": task_id,
        "task_type": task_type,
        "run_id": run_id,
        "registry": index.relative_to(root).as_posix(),
        "registry_sha256_at_publish": hashlib.sha256(index.read_bytes()).hexdigest(),
        "rows": [
            {
                "entry_id": row.entry_id,
                "scope_key": row.scope_key,
                "file": (Path("docs") / directory_name / row.file).as_posix(),
                "file_sha256": row.file_sha256,
                "source": row.story,
                "supersedes": list(row.supersedes),
            }
            for row in published
        ],
    }

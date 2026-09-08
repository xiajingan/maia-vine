"""Explicit, reversible migration for project-owned document scope registries."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mai_harness.runtime.domain.document_registry import (
    REGISTRY_COLUMNS,
    REGISTRY_DIRECTORIES,
    registry_link_target,
    registry_markdown_table,
    validate_registry,
)
from mai_harness.runtime.infrastructure.core.state_store import StateStore

LEGACY_COLUMNS = {
    directory: {
        tuple(column for column in columns if column not in {"SHA-256", "Task ID", "Run ID"}),
        tuple(column for column in columns if column != "SHA-256"),
    }
    for directory, columns in REGISTRY_COLUMNS.items()
}
LEGACY_SIMPLE_COLUMNS = {
    "product-specs": ("文件", "功能域", "关联 Story", "Sprint 覆盖", "验证状态"),
    "design-docs": ("文件", "功能", "关联需求", "验证状态"),
    "tech-docs": ("文件", "模块", "关联需求", "验证状态"),
}
EMPTY = {"", "—", "-", "_(待生成)_"}
MAPPING_COMMON_KEYS = {
    "directory",
    "file",
    "entry_id",
    "scope_key",
    "module",
    "source",
    "sprint",
    "status",
    "supersedes",
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replace_table(
    content: str,
    old_headers: list[str],
    new_headers: list[str],
    rows: list[dict[str, str]],
) -> str:
    lines = content.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if [cell.strip() for cell in line.strip().strip("|").split("|")] == old_headers
    )
    end = start + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    separator = "|" + "|".join("---" for _ in new_headers) + "|"
    rendered = ["| " + " | ".join(str(row.get(header, "—")) for header in new_headers) + " |" for row in rows]
    return (
        "\n".join(
            [
                *lines[:start],
                "| " + " | ".join(new_headers) + " |",
                separator,
                *rendered,
                *lines[end:],
            ]
        ).rstrip()
        + "\n"
    )


def _confirmed_mapping(
    root: Path,
    mapping_path: Path | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str] | None]:
    if mapping_path is None:
        return {}, None
    resolved = (root / mapping_path).resolve() if not mapping_path.is_absolute() else mapping_path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("--mapping 必须指向当前工程内存在的 YAML 文件")
    try:
        document = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"旧索引迁移 mapping 无法解析: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "entries"} or document.get("version") != 1:
        raise ValueError("旧索引迁移 mapping 必须严格包含 version: 1 与 entries")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("旧索引迁移 mapping.entries 必须是数组")
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise ValueError(f"旧索引迁移 mapping.entries[{index}] 必须是对象")
        directory = item.get("directory")
        file = item.get("file")
        key = (str(directory), str(file))
        if directory not in REGISTRY_DIRECTORIES or not isinstance(file, str) or not file:
            raise ValueError(f"旧索引迁移 mapping.entries[{index}] 的 directory/file 非法")
        expected = MAPPING_COMMON_KEYS | (
            {"page_or_area"}
            if directory in {"product-specs", "design-docs"}
            else {"tech_kind", "table_or_api", "component"}
        )
        if set(item) != expected:
            raise ValueError(f"旧索引迁移 mapping.entries[{index}] 字段必须严格为: {sorted(expected)}")
        if key in mapped:
            raise ValueError(f"旧索引迁移 mapping 重复: {directory}/{file}")
        text_fields = expected - {"directory", "file", "supersedes"}
        if not all(isinstance(item.get(field), str) and item[field].strip() for field in text_fields):
            raise ValueError(f"旧索引迁移 mapping.entries[{index}] 的语义字段必须是非空字符串")
        if not isinstance(item.get("supersedes"), list) or not all(
            isinstance(value, str) and value.strip() for value in item["supersedes"]
        ):
            raise ValueError(f"旧索引迁移 mapping.entries[{index}].supersedes 必须是数组")
        mapped[key] = item
    return mapped, {
        "path": resolved.relative_to(root.resolve()).as_posix(),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def _legacy_simple_row(
    index: Path,
    directory_name: str,
    raw: dict[str, str],
    migration_run: str,
    mapping: dict[tuple[str, str], dict[str, Any]],
    used_mappings: set[tuple[str, str]],
) -> dict[str, str]:
    file_value = str(raw.get("文件", "")).strip()
    if file_value in EMPTY:
        return {column: "—" for column in REGISTRY_COLUMNS[directory_name]}
    relative = Path(registry_link_target(file_value))
    document = (index.parent / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not document.is_relative_to(index.parent.resolve())
        or not document.is_file()
    ):
        raise ValueError(f"旧作用域索引文件不存在或越界: {index}: {relative}")
    key = (directory_name, relative.as_posix())
    confirmed = mapping.get(key)
    if confirmed is None:
        raise ValueError(
            f"旧简单索引不能推断 Scope Key、页面/API/组件等语义；"
            f"请通过 --mapping 为 {directory_name}/{relative.as_posix()} 显式确认"
        )
    used_mappings.add(key)
    supersedes = confirmed["supersedes"]
    common = {
        "Entry ID": str(confirmed["entry_id"]),
        "Scope Key": str(confirmed["scope_key"]),
        "文件": file_value,
        "SHA-256": hashlib.sha256(document.read_bytes()).hexdigest(),
        "模块": str(confirmed["module"]),
        "关联 Story/AC": str(confirmed["source"]),
        "Sprint": str(confirmed["sprint"]),
        "Task ID": "registry-migration",
        "Run ID": migration_run,
        "状态": str(confirmed["status"]),
        "Supersedes": ", ".join(str(value) for value in supersedes) if supersedes else "—",
    }
    if directory_name in {"product-specs", "design-docs"}:
        common["页面/功能区域"] = str(confirmed["page_or_area"])
    else:
        common.update(
            {
                "方案类型": str(confirmed["tech_kind"]),
                "表/API": str(confirmed["table_or_api"]),
                "组件": str(confirmed["component"]),
            }
        )
    return common


def migrate_document_registries(
    root: Path,
    *,
    mapping_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Migrate legacy indexes without overwriting them silently during framework install."""
    results: list[dict[str, Any]] = []
    mapping, mapping_evidence = _confirmed_mapping(root, mapping_path)
    used_mappings: set[tuple[str, str]] = set()
    migration_store = StateStore(root / ".harness/state/document-registry/migrations")
    migrations: list[dict[str, Any]] = []
    simple_directories: set[str] = set()
    for directory_name in sorted(REGISTRY_DIRECTORIES):
        index = root / "docs" / directory_name / "index.md"
        if not index.is_file():
            continue
        original = index.read_text(encoding="utf-8")
        headers, raw_rows = registry_markdown_table(original)
        expected = REGISTRY_COLUMNS[directory_name]
        if tuple(headers) == expected:
            results.append({"directory": directory_name, "status": "already-current"})
            continue
        simple_legacy = tuple(headers) == LEGACY_SIMPLE_COLUMNS[directory_name]
        if simple_legacy:
            simple_directories.add(directory_name)
        if tuple(headers) not in LEGACY_COLUMNS[directory_name] and not simple_legacy:
            raise ValueError(f"{index} 不是可识别的旧作用域索引，拒绝自动推断")
        before_sha = _sha(original)
        migration_run = before_sha[:32]
        migrated_rows: list[dict[str, str]] = []
        for _, raw in raw_rows:
            if simple_legacy:
                migrated_rows.append(
                    _legacy_simple_row(
                        index,
                        directory_name,
                        raw,
                        migration_run,
                        mapping,
                        used_mappings,
                    )
                )
                continue
            row = dict(raw)
            entry_id = str(row.get("Entry ID", "")).strip()
            if entry_id not in {"", "—", "-", "_(待生成)_"}:
                relative = Path(registry_link_target(str(row.get("文件", ""))))
                document = (index.parent / relative).resolve()
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not document.is_relative_to(index.parent.resolve())
                    or not document.is_file()
                ):
                    raise ValueError(f"旧作用域索引文件不存在或越界: {index}: {relative}")
                row["SHA-256"] = hashlib.sha256(document.read_bytes()).hexdigest()
                row.setdefault("Task ID", "registry-migration")
                row.setdefault("Run ID", migration_run)
            else:
                row.setdefault("SHA-256", "—")
                row.setdefault("Task ID", "—")
                row.setdefault("Run ID", "—")
            migrated_rows.append(row)
        migrated = _replace_table(original, headers, list(expected), migrated_rows)
        backup_name = f"{directory_name}.before.md"
        receipt_name = f"{directory_name}.json"
        existing = migration_store.read_json(receipt_name, None)
        if isinstance(existing, dict) and existing.get("before_sha256") != before_sha:
            raise ValueError(f"{directory_name} 已存在不同基线的迁移记录，拒绝覆盖")
        migrations.append(
            {
                "directory": directory_name,
                "index": index,
                "original": original,
                "migrated": migrated,
                "backup_name": backup_name,
                "receipt_name": receipt_name,
                "before_sha256": before_sha,
                "migration_run_id": migration_run,
                "mapping": mapping_evidence if simple_legacy else None,
            }
        )
    unused_mappings = sorted(key for key in set(mapping) - used_mappings if key[0] in simple_directories)
    if unused_mappings:
        raise ValueError(f"旧索引迁移 mapping 存在未匹配条目: {unused_mappings}")
    mutation_paths = [
        path
        for item in migrations
        for path in (
            migration_store.path(item["backup_name"]),
            item["index"],
            migration_store.path(item["receipt_name"]),
        )
    ]
    snapshot = {path: path.read_bytes() if path.is_file() else None for path in mutation_paths}
    try:
        for item in migrations:
            migration_store.write_text(item["backup_name"], item["original"])
            StateStore(item["index"].parent).write_text(item["index"].name, item["migrated"])
        validation_errors = [error for item in migrations for error in validate_registry(root, item["directory"])]
        if validation_errors:
            raise ValueError("作用域索引迁移结果无效:\n- " + "\n- ".join(validation_errors))
        for item in migrations:
            receipt = {
                "schema_version": 1,
                "directory": item["directory"],
                "status": "migrated",
                "before_sha256": item["before_sha256"],
                "after_sha256": _sha(item["migrated"]),
                "backup": migration_store.path(item["backup_name"]).relative_to(root).as_posix(),
                "migration_run_id": item["migration_run_id"],
                "mapping": item["mapping"],
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            migration_store.write_json(item["receipt_name"], receipt)
            results.append(receipt)
    except Exception:
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                StateStore(path.parent).write_text(path.name, content.decode("utf-8"))
        raise
    return results


def rollback_document_registry_migrations(root: Path) -> list[dict[str, Any]]:
    """Restore only indexes that still equal the migration output."""
    results: list[dict[str, Any]] = []
    migration_store = StateStore(root / ".harness/state/document-registry/migrations")
    rollbacks: list[dict[str, Any]] = []
    for directory_name in sorted(REGISTRY_DIRECTORIES):
        receipt_name = f"{directory_name}.json"
        receipt = migration_store.read_json(receipt_name, None)
        if not isinstance(receipt, dict):
            continue
        index = root / "docs" / directory_name / "index.md"
        backup = root / str(receipt.get("backup", ""))
        if not index.is_file() or not backup.is_file():
            raise ValueError(f"{directory_name} 缺少可回退的索引或备份")
        if hashlib.sha256(backup.read_bytes()).hexdigest() != receipt.get("before_sha256"):
            raise ValueError(f"{directory_name} 迁移备份已变化，拒绝回退")
        current_sha = hashlib.sha256(index.read_bytes()).hexdigest()
        if current_sha == receipt.get("before_sha256"):
            results.append({"directory": directory_name, "status": "already-rolled-back"})
            continue
        if current_sha != receipt.get("after_sha256"):
            raise ValueError(f"{directory_name} 迁移后已被修改，拒绝覆盖式回退")
        rollbacks.append(
            {
                "directory": directory_name,
                "index": index,
                "backup": backup,
                "receipt_name": receipt_name,
                "receipt": receipt,
            }
        )
    mutation_paths = [
        path
        for item in rollbacks
        for path in (item["index"], migration_store.path(item["receipt_name"]))
    ]
    snapshot = {path: path.read_bytes() if path.is_file() else None for path in mutation_paths}
    try:
        for item in rollbacks:
            StateStore(item["index"].parent).write_text(
                item["index"].name,
                item["backup"].read_text(encoding="utf-8"),
            )
        for item in rollbacks:
            receipt = {
                **item["receipt"],
                "status": "rolled-back",
                "rolled_back_at": datetime.now(UTC).isoformat(),
            }
            migration_store.write_json(item["receipt_name"], receipt)
            results.append(receipt)
    except Exception:
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                StateStore(path.parent).write_text(path.name, content.decode("utf-8"))
        raise
    return results

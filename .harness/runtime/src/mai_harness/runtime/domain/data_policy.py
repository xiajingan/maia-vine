"""Project-owned, product-neutral static database and business ID policy."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any


def validate_data_policy(policy: Any) -> list[str]:
    if policy is None:
        return []
    if not isinstance(policy, dict):
        return ["data_policy: 必须是对象或 null"]
    errors: list[str] = []
    allowed = {
        "sources",
        "database",
        "business_ids",
        "forbidden_database_objects",
        "forbidden_sql_functions",
        "forbidden_sql_features",
        "forbid_drizzle_relations",
    }
    if unknown := set(policy) - allowed:
        errors.append(f"data_policy: 未知字段 {sorted(map(str, unknown))}")
    for key in ("sources", "forbidden_database_objects", "forbidden_sql_functions", "forbidden_sql_features"):
        values = policy.get(key, [] if key != "sources" else None)
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            errors.append(f"data_policy.{key}: 必须为字符串数组")
    sources = policy.get("sources")
    if isinstance(sources, list):
        if not sources:
            errors.append("data_policy.sources: 不能为空")
        for source in sources:
            if not isinstance(source, str):
                continue
            path = PurePosixPath(source)
            if path.is_absolute() or ".." in path.parts or source in {"", "."} or "\\" in source:
                errors.append("data_policy.sources: 必须为工程内相对目录或文件")
    database = policy.get("database")
    if database is not None:
        fields = {"engine", "version", "dialect", "dialect_version", "orm", "orm_version"}
        if not isinstance(database, dict) or set(database) != fields:
            errors.append("data_policy.database: 必须完整声明 engine/version/dialect/dialect_version/orm/orm_version")
        elif any(not isinstance(value, str) or not value.strip() for value in database.values()):
            errors.append("data_policy.database: 版本与名称必须为非空字符串")
    if not isinstance(policy.get("forbid_drizzle_relations", False), bool):
        errors.append("data_policy.forbid_drizzle_relations: 必须为布尔值")
    choices = {
        "forbidden_database_objects": {"procedure", "function", "trigger", "event"},
        "forbidden_sql_features": {"fulltext", "lateral", "skip_locked", "tidb_extensions", "json_operators"},
    }
    for key, supported in choices.items():
        if isinstance(policy.get(key, []), list) and any(
            not isinstance(v, str) or v not in supported for v in policy.get(key, [])
        ):
            errors.append(f"data_policy.{key}: 存在未知规则")
    if database is None and any(
        policy.get(key) for key in (*choices, "forbidden_sql_functions", "forbid_drizzle_relations")
    ):
        errors.append("data_policy.database: SQL/ORM 规则启用时必须声明数据库契约")
    ids = policy.get("business_ids")
    if ids is not None:
        if not isinstance(ids, dict):
            errors.append("data_policy.business_ids: 必须是对象或 null")
        else:
            fields = {
                "strategy",
                "scope",
                "storage_type",
                "wire_type",
                "fields",
                "exclude_fields",
                "forbidden_generators",
            }
            if set(ids) != fields:
                errors.append(f"data_policy.business_ids: 必须声明 {', '.join(sorted(fields))}")
            for key in ("strategy", "scope", "storage_type", "wire_type"):
                if not isinstance(ids.get(key), str) or not ids[key].strip():
                    errors.append(f"data_policy.business_ids.{key}: 必须是非空字符串")
            for key in ("fields", "exclude_fields", "forbidden_generators"):
                values = ids.get(key)
                if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                    errors.append(f"data_policy.business_ids.{key}: 必须为字符串数组")
            if not ids.get("fields"):
                errors.append("data_policy.business_ids.fields: 不能为空")
    return errors


def is_business_id(name: str, policy: dict) -> bool:
    ids = policy.get("business_ids")
    return bool(
        ids
        and any(fnmatchcase(name, p) for p in ids["fields"])
        and not any(fnmatchcase(name, p) for p in ids["exclude_fields"])
    )

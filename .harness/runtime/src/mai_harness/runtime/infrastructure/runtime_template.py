"""Resolve deployment runtime.env templates from declared configuration sources."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

RUNTIME_OPTIONAL_KEYS = {"TEST_PUBLIC_BASE_URL", "PROD_PUBLIC_BASE_URL"}


def _read(env: Mapping[str, str], key: str) -> str:
    return env.get(key, "")


def _rewrite_host(value: str, host: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
            return value
        credentials = ""
        if parsed.username:
            credentials = parsed.username + (f":{parsed.password}" if parsed.password else "") + "@"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{credentials}{host}{port}", parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return value


def derive_image_repositories(project_prefix: str, env_map: Mapping[str, str]) -> dict[str, str]:
    repo = _read(env_map, "HARNESS_IMAGE_REPO")
    prefix = f"{project_prefix}_" if project_prefix else ""
    return {
        "api": _read(env_map, f"{prefix}API_IMAGE_REPOSITORY") or (f"{repo}-api" if repo else ""),
        "web": _read(env_map, f"{prefix}WEB_IMAGE_REPOSITORY") or (f"{repo}-web" if repo else ""),
    }


def build_runtime_map(environment: str, tag: str, env_map: Mapping[str, str]) -> dict[str, str]:
    prefix = f"{environment.upper()}_"
    database_url = _rewrite_host(_read(env_map, f"{prefix}DATABASE_URL") or _read(env_map, "DATABASE_URL"), "postgres")
    redis_url = _rewrite_host(_read(env_map, f"{prefix}REDIS_URL") or _read(env_map, "REDIS_URL"), "redis")
    parsed = urlsplit(database_url) if database_url else None
    public = _read(env_map, f"{prefix}PUBLIC_BASE_URL")
    api = _read(env_map, f"{prefix}API_BASE_URL")
    brokers = ",".join(
        f"kafka:{item.partition(':')[2] or '9092'}"
        if item.partition(":")[0].strip().lower() in {"localhost", "127.0.0.1", "::1"}
        else item.strip()
        for item in _read(env_map, "KAFKA_BROKERS").split(",")
        if item.strip()
    )
    return {
        f"{prefix}PUBLIC_BASE_URL": public,
        f"{prefix}API_BASE_URL": api,
        "OAUTH_CALLBACK_URL": _read(env_map, "OAUTH_CALLBACK_URL")
        or ((public or api).rstrip("/") + "/auth/callback" if public or api else ""),
        "POSTGRES_USER": _read(env_map, "POSTGRES_USER") or (parsed.username if parsed else "") or "",
        "POSTGRES_PASSWORD": _read(env_map, "POSTGRES_PASSWORD") or (parsed.password if parsed else "") or "",
        "POSTGRES_DB": _read(env_map, "POSTGRES_DB") or ((parsed.path.lstrip("/")) if parsed else ""),
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "KAFKA_BROKERS": brokers or "kafka:9092",
        "MINIO_ROOT_USER": _read(env_map, "MINIO_ROOT_USER")
        or _read(env_map, f"{prefix}MINIO_ACCESS_KEY")
        or _read(env_map, "MINIO_ACCESS_KEY"),
        "MINIO_ROOT_PASSWORD": _read(env_map, "MINIO_ROOT_PASSWORD")
        or _read(env_map, f"{prefix}MINIO_SECRET_KEY")
        or _read(env_map, "MINIO_SECRET_KEY"),
        "MINIO_ENDPOINT": "minio"
        if _read(env_map, "MINIO_ENDPOINT").lower() in {"", "localhost", "127.0.0.1", "::1"}
        else _read(env_map, "MINIO_ENDPOINT"),
        "MINIO_PORT": _read(env_map, "MINIO_PORT") or "9000",
        "MINIO_ACCESS_KEY": _read(env_map, f"{prefix}MINIO_ACCESS_KEY") or _read(env_map, "MINIO_ACCESS_KEY"),
        "MINIO_SECRET_KEY": _read(env_map, f"{prefix}MINIO_SECRET_KEY") or _read(env_map, "MINIO_SECRET_KEY"),
        "MINIO_BUCKET": _read(env_map, "MINIO_BUCKET"),
        "MINIO_USE_SSL": _read(env_map, "MINIO_USE_SSL") or "false",
        "MINIO_PUBLIC_BASE_URL": _read(env_map, f"{prefix}MINIO_PUBLIC_BASE_URL")
        or _read(env_map, "MINIO_PUBLIC_BASE_URL"),
        "JWT_SECRET": _read(env_map, f"{prefix}JWT_SECRET"),
    }


def parse_runtime_template(template: str) -> list[dict[str, Any]]:
    output = []
    for number, line in enumerate(template.splitlines(), 1):
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            output.append({"line": number, "key": match.group(1), "fallback": match.group(2)})
    return output


def source_candidates(environment: str, key: str) -> list[str]:
    prefix = f"{environment.upper()}_"
    values = {key} if key.startswith(prefix) else {prefix + key, key}
    mappings = {
        "OAUTH_CALLBACK_URL": {"OAUTH_CALLBACK_URL", prefix + "PUBLIC_BASE_URL", prefix + "API_BASE_URL"},
        "DATABASE_URL": {prefix + "DATABASE_URL"},
        "REDIS_URL": {prefix + "REDIS_URL"},
        "JWT_SECRET": {prefix + "JWT_SECRET"},
    }
    values |= mappings.get(key, set())
    if key in {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}:
        values |= {key, prefix + "DATABASE_URL", "DATABASE_URL"}
    if key in {"MINIO_ROOT_USER", "MINIO_ACCESS_KEY"}:
        values |= {prefix + "MINIO_ACCESS_KEY", "MINIO_ROOT_USER", "MINIO_ACCESS_KEY"}
    if key in {"MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"}:
        values |= {prefix + "MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"}
    return sorted(values)


def resolve_value(
    key: str, fallback: str, environment: str, tag: str, runtime: dict[str, str], env_map: Mapping[str, str]
) -> str:
    if runtime.get(key):
        return runtime[key]
    prefix = f"{environment.upper()}_"
    if prefix + key in env_map:
        return env_map[prefix + key]
    if key in env_map:
        return env_map[key]
    if key.endswith("_IMAGE_TAG"):
        return tag or fallback
    match = re.match(r"^(.+)_(API|WEB)_IMAGE_REPOSITORY$", key)
    if match:
        return derive_image_repositories(match.group(1), env_map)[match.group(2).lower()] or fallback
    return fallback


def analyze_runtime_template(
    environment: str,
    template_path: str | Path,
    *,
    env_map: Mapping[str, str],
    declared_secrets: list[str] | None = None,
    template: str | None = None,
    allow_local_runtime_file_fallback: bool = False,
    tag: str = "<validate>",
) -> dict[str, Any]:
    text = template if template is not None else Path(template_path).read_text(encoding="utf-8")
    runtime = build_runtime_map(environment, tag, env_map)
    declared = set(declared_secrets or [])
    missing = []
    for entry in parse_runtime_template(text):
        key = entry["key"]
        if (
            entry["fallback"]
            or key in RUNTIME_OPTIONAL_KEYS
            or key.endswith("_IMAGE_TAG")
            or re.match(r"^.+_(API|WEB)_IMAGE_REPOSITORY$", key)
        ):
            continue
        if resolve_value(key, "", environment, tag, runtime, env_map):
            continue
        candidates = source_candidates(environment, key)
        if not any(
            item in declared
            or bool(env_map.get(item))
            or (allow_local_runtime_file_fallback and not item.startswith(environment.upper() + "_"))
            for item in candidates
        ):
            missing.append({**entry, "candidates": candidates})
    return {"missing_sources": missing}

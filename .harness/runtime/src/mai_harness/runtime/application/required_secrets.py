"""Resolve framework and project-required deployment secret names."""

from __future__ import annotations

from typing import Any

from mai_harness.runtime.infrastructure.utils import fatal, warn

COMMON = ["DEPLOY_USER", "DEPLOY_HOST", "DEPLOY_PORT", "DEPLOY_WORKDIR", "API_BASE_URL", "SSH_PRIVATE_KEY"]
PROD_EXTRA = ["REGISTRY_USERNAME", "REGISTRY_PASSWORD"]


def base_required_secrets(env_name: str) -> list[str]:
    if not isinstance(env_name, str) or not env_name:
        fatal(f"base_required_secrets: env_name 必须为非空字符串，实际：{env_name}")
    suffixes = COMMON + (PROD_EXTRA if env_name == "prod" else [])
    return [f"{env_name.upper()}_{suffix}" for suffix in suffixes]


def analyze_required_secrets(env_name: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"schema_errors": [f"environments.{env_name} 节点缺失或非对象"], "warnings": [], "secrets": []}
    legacy = entry.get("required_secrets") if isinstance(entry.get("required_secrets"), list) else []
    has_extra = isinstance(entry.get("extra_required_secrets"), list)
    if legacy and has_extra:
        return {
            "schema_errors": [f"environments.{env_name}: 不可同时声明 required_secrets 与 extra_required_secrets"],
            "warnings": [],
            "secrets": [],
        }
    if legacy:
        return {
            "schema_errors": [],
            "warnings": [f"environments.{env_name}.required_secrets 是 legacy 字段；建议改用 extra_required_secrets"],
            "secrets": list(legacy),
        }
    extra = entry.get("extra_required_secrets", []) if has_extra else []
    return {
        "schema_errors": [],
        "warnings": [],
        "secrets": list(dict.fromkeys(base_required_secrets(env_name) + extra)),
    }


def resolve_required_secrets(env_name: str, entry: dict[str, Any] | None) -> list[str]:
    result = analyze_required_secrets(env_name, entry)
    if result["schema_errors"]:
        fatal("\n".join(result["schema_errors"]))
    for message in result["warnings"]:
        warn(message)
    return result["secrets"]

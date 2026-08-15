#!/usr/bin/env python3
"""Validate deployment environment schema and runtime inputs."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.required_secrets import analyze_required_secrets
from mai_harness.runtime.infrastructure.deploy_config import load_environments_compat
from mai_harness.runtime.infrastructure.local_runtime_env import load_local_runtime_env_snapshot
from mai_harness.runtime.infrastructure.runtime_template import analyze_runtime_template
from mai_harness.runtime.infrastructure.secrets_file import expected_secrets_source, load_secrets_file_snapshot

COMMON_FIELDS = ("enabled", "health_url", "secrets_source")
DOCKER_FIELDS = (
    "deploy_target",
    "remote_workdir",
    "compose_file",
    "remote_compose_file",
    "remote_runtime_env_file",
)
CLOUD_NATIVE_FIELDS = ("context", "cluster_identity", "namespace", "helm_releases", "credential_refs")
SCHEMA = {
    "test": COMMON_FIELDS,
    "prod": COMMON_FIELDS + ("health_window_minutes", "rollback_strategy", "rollback_thresholds"),
}
REQUIRED_DEPLOY_INPUTS = {
    "test": ("TEST_DEPLOY_USER", "TEST_DEPLOY_HOST", "TEST_DEPLOY_PORT", "TEST_DEPLOY_WORKDIR", "TEST_API_BASE_URL"),
    "prod": ("PROD_DEPLOY_USER", "PROD_DEPLOY_HOST", "PROD_DEPLOY_PORT", "PROD_DEPLOY_WORKDIR", "PROD_API_BASE_URL"),
}


@dataclass
class ValidationIssues:
    schema_errors: list[str] = field(default_factory=list)
    secret_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def runtime_template_path(entry: dict[str, Any]) -> str:
    value = entry.get("compose_file")
    return (
        value.replace("docker-compose.yml", "runtime.env.example").replace("docker-compose.yaml", "runtime.env.example")
        if isinstance(value, str)
        else ""
    )


def secret_provided(name: str, env_map: Mapping[str, str]) -> bool:
    if env_map.get(name):
        return True
    key_path = env_map.get("HARNESS_SSH_KEY_PATH", "").strip()
    return name.endswith("_SSH_PRIVATE_KEY") and bool(key_path and Path(key_path).exists())


def collect_validation_issues(
    cfg: dict[str, Any],
    env_map: Mapping[str, str],
    *,
    require_secrets: bool = True,
    environment: str | None = None,
    runtime_templates: dict[str, str] | None = None,
    runtime_template_content: dict[str, str] | None = None,
) -> ValidationIssues:
    result = ValidationIssues()
    templates = runtime_templates or {}
    contents = runtime_template_content or {}
    if cfg.get("promote_strategy") not in {"manual", "train"}:
        result.schema_errors.append(f"promote_strategy 必须是 manual 或 train，当前：{cfg.get('promote_strategy')}")
    for name, required_fields in SCHEMA.items():
        if environment and name != environment:
            continue
        entry = (cfg.get("environments") or {}).get(name)
        if not entry:
            result.schema_errors.append(f"environments.{name} 缺失")
            continue
        if not entry.get("enabled"):
            continue
        deploy_mode = entry.get("deploy_mode", "docker")
        if deploy_mode not in {"docker", "cloud-native"}:
            result.schema_errors.append(f"environments.{name}.deploy_mode 必须是 docker 或 cloud-native")
            continue
        mode_fields = DOCKER_FIELDS if deploy_mode == "docker" else CLOUD_NATIVE_FIELDS
        for key in (*required_fields, *mode_fields):
            if entry.get(key) in (None, ""):
                result.schema_errors.append(f"environments.{name}.{key} 缺失或为空")
        expected = expected_secrets_source(name)
        if entry.get("secrets_source") and entry["secrets_source"] != expected:
            result.schema_errors.append(
                f"environments.{name}.secrets_source 必须为 {expected}，当前：{entry['secrets_source']}"
            )
        if deploy_mode == "docker":
            for key, prefix in (
                ("compose_file", f"deploy/{name}/"),
                ("remote_compose_file", f"deploy/{name}/"),
                ("remote_runtime_env_file", f"deploy/{name}/"),
            ):
                if isinstance(entry.get(key), str) and not entry[key].startswith(prefix):
                    result.schema_errors.append(f"environments.{name}.{key} 必须指向 {prefix} 资产")
            if isinstance(entry.get("deploy_target"), str) and not entry["deploy_target"].startswith("ssh://"):
                result.schema_errors.append(f"environments.{name}.deploy_target 必须使用 ssh:// 目标声明")
            analyzed = analyze_required_secrets(name, entry)
            result.schema_errors += analyzed["schema_errors"]
            result.warnings += analyzed["warnings"]
            missing_inputs = [key for key in REQUIRED_DEPLOY_INPUTS[name] if key not in analyzed["secrets"]]
            if missing_inputs:
                result.schema_errors.append(
                    f"environments.{name}.resolved_secrets 缺少部署输入：{', '.join(missing_inputs)}"
                )
        else:
            analyzed = {"secrets": list(entry.get("credential_refs", [])), "schema_errors": [], "warnings": []}
            releases = entry.get("helm_releases", [])
            if not isinstance(releases, list) or not releases:
                result.schema_errors.append(f"environments.{name}.helm_releases 至少声明一个 Helm release")
            else:
                for index, release in enumerate(releases):
                    for key in ("name", "chart", "version"):
                        if not isinstance(release, dict) or not release.get(key):
                            result.schema_errors.append(f"environments.{name}.helm_releases[{index}].{key} 缺失")
        if require_secrets:
            missing = [key for key in analyzed["secrets"] if not secret_provided(key, env_map)]
            if missing:
                result.secret_errors.append(f"environments.{name} 缺少 secret：{', '.join(missing)}")
        template_path = (templates.get(name) or runtime_template_path(entry)) if deploy_mode == "docker" else ""
        if template_path:
            if not Path(template_path).exists() and name not in contents:
                result.schema_errors.append(f"environments.{name}.runtime_template 未找到：{template_path}")
            else:
                analysis = analyze_runtime_template(
                    name,
                    template_path,
                    env_map=env_map,
                    declared_secrets=analyzed["secrets"],
                    template=contents.get(name),
                    allow_local_runtime_file_fallback=not require_secrets,
                )
                for item in analysis["missing_sources"]:
                    result.schema_errors.append(
                        f"environments.{name}.runtime_template {template_path}:{item['line']} {item['key']} 为空且无配置来源（声明 {' 或 '.join(item['candidates'])}）"
                    )
        if name == "prod":
            for key in ("error_rate_5xx", "p95_latency_ms", "cpu_saturation"):
                if not isinstance((entry.get("rollback_thresholds") or {}).get(key), (int, float)):
                    result.schema_errors.append(f"environments.prod.rollback_thresholds.{key} 必须为数字")
    return result


def load_config() -> dict[str, Any]:
    config = load_environments_compat()
    if config.get("version") != 1:
        raise ValueError("config/deploy.yml: version 必须为 1")
    return config


def validate_command(mode: str, environment: str | None) -> int:
    cfg = load_config()
    runtime_errors: list[str] = []
    env_map = {} if mode == "schema" else dict(os.environ)
    if mode == "runtime":
        env_map.update(load_local_runtime_env_snapshot()["values"])
        for name, entry in (cfg.get("environments") or {}).items():
            if (environment and name != environment) or not entry.get("enabled"):
                continue
            required = (
                list(entry.get("credential_refs", []))
                if entry.get("deploy_mode", "docker") == "cloud-native"
                else analyze_required_secrets(name, entry)["secrets"]
            )
            snapshot = load_secrets_file_snapshot(name, required)
            if not snapshot["exists"]:
                runtime_errors.append(f"environments.{name} secrets 文件不存在：{expected_secrets_source(name)}")
            elif snapshot["error"]:
                runtime_errors.append(f"environments.{name} secrets 文件无法加载：{snapshot['error']}")
            else:
                env_map.update(snapshot["values"])
    issues = collect_validation_issues(cfg, env_map, require_secrets=mode == "runtime", environment=environment)
    for message in issues.warnings:
        print(f"⚠️  {message}")
    for message in issues.schema_errors + issues.secret_errors + runtime_errors:
        print(f"❌ {message}")
    if issues.schema_errors or issues.secret_errors or runtime_errors:
        return 1
    print(f"✅ deploy config {mode} 校验通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--mode", choices=("schema", "runtime"), default="schema")
    validate.add_argument("--env", choices=SCHEMA)
    for command in ("check", "print"):
        item = sub.add_parser(command)
        item.add_argument("env")
    args = parser.parse_args()
    try:
        cfg = load_config()
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ {exc}")
        return 1
    if args.command == "validate":
        return validate_command(args.mode, args.env)
    entry = (cfg.get("environments") or {}).get(args.env)
    if args.command == "print":
        if entry is None:
            print(f"❌ environments.{args.env} 不存在")
            return 1
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0
    if not entry or not entry.get("enabled"):
        print(f"{args.env}: 未启用（enabled=false）")
        return 2
    print(f"✅ {args.env}: 已启用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate and materialize deployment inputs immediately before promotion."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.promotion_preflight import (
    promotion_input_identity,
    promotion_preflight_filename,
)
from mai_harness.runtime.application.required_secrets import analyze_required_secrets
from mai_harness.runtime.application.task_evidence import current_attempt_state_path
from mai_harness.runtime.commands.env_check import collect_validation_issues
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import (
    get_environment,
    load_build_targets_compat,
    load_deploy_config,
)
from mai_harness.runtime.infrastructure.harness_config import assert_deploy_mode_implemented, load_harness_config
from mai_harness.runtime.infrastructure.local_runtime_env import load_local_runtime_env_snapshot
from mai_harness.runtime.infrastructure.secrets_file import ensure_secrets_file, load_secrets_file_snapshot
from mai_harness.runtime.infrastructure.utils import write_yaml


def deployment_asset_check(environment: str, entry: dict, harness: dict) -> tuple[str, Path, list[str]]:
    mode = entry.get("deploy_mode") or harness["deploy"].get(f"{environment}_mode", "docker")
    assert_deploy_mode_implemented(mode, environment)
    if mode == "cloud-native":
        plan = Path(f".harness/state/helm-plan-{environment}.yml")
        releases = entry.get("helm_releases", [])
        issues = []
        for release in releases:
            chart = Path(release["chart"])
            if not chart.exists():
                issues.append(f"Helm chart 不存在: {chart}")
            for values_file in release.get("values_files", []):
                if not Path(values_file).exists():
                    issues.append(f"Helm values 不存在: {values_file}")
        write_yaml(
            plan,
            {
                "environment": environment,
                "context": entry.get("context", ""),
                "cluster_identity": entry.get("cluster_identity", ""),
                "namespace": entry.get("namespace", ""),
                "releases": releases,
            },
        )
        return mode, plan, issues
    compose = Path(entry.get("compose_file", f"deploy/{environment}/docker-compose.yml"))
    issues = []
    if not compose.exists():
        build = load_build_targets_compat()
        services = {
            name: {
                "image": f"{build.get('image_repo', 'TODO-REPO')}-{name}:${{IMAGE_TAG}}",
                "restart": "unless-stopped",
                "env_file": [f".env.{environment}"],
            }
            for name in (build.get("targets") or {"app": {}})
        }
        write_yaml(compose, {"services": services})
        issues.append(f"首次生成 {compose} stub，请补全后重跑")
    elif "services:" not in compose.read_text(encoding="utf-8"):
        issues.append(f"{compose} 缺少 services")
    return mode, compose, issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env", choices=("test", "prod"))
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--sprint")
    parser.add_argument("--task-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    root = Path.cwd()
    binding_values = (args.sprint, args.task_id, args.run_id)
    if any(binding_values) and not all(binding_values):
        parser.error("--sprint、--task-id 与 --run-id 必须同时提供")
    task_binding = None
    if all(binding_values):
        sprint = root / "docs/exec-plans/active" / f"{Path(args.sprint).name}.md"
        if not sprint.is_file() or args.sprint != sprint.stem:
            parser.error(f"当前 active Sprint 计划不存在: {args.sprint}")
        attempt_path = current_attempt_state_path(root, sprint, args.task_id)
        try:
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parser.error(f"promote-test attempt 无法读取: {attempt_path}: {exc}")
        if (
            attempt.get("schema_version") != 3
            or attempt.get("sprint") != sprint.stem
            or attempt.get("task_id") != args.task_id
            or attempt.get("task_type") != "promote-test"
            or attempt.get("status") != "pending"
            or attempt.get("run_id") != args.run_id
            or attempt.get("review") is not None
        ):
            parser.error("promote-prep 必须绑定当前 pending promote-test attempt")
        task_binding = {
            "sprint": sprint.stem,
            "task_id": args.task_id,
            "attempt_run_id": attempt["run_id"],
            "attempt": attempt["attempt"],
        }
    issues = []
    try:
        deploy, harness, entry = load_deploy_config(), load_harness_config(), get_environment(args.env)
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ {exc}")
        return 1
    mode = entry.get("deploy_mode") or harness["deploy"].get(f"{args.env}_mode", "docker")
    required_fields = (
        ("deploy_target", "remote_workdir", "health_url", "secrets_source")
        if mode == "docker"
        else (
            "context",
            "cluster_identity",
            "namespace",
            "helm_releases",
            "credential_refs",
            "health_url",
            "secrets_source",
        )
    )
    for key in required_fields:
        if not entry.get(key):
            issues.append({"kind": "env-config", "detail": f"environments.{args.env}.{key} 缺失"})
    if not entry.get("enabled"):
        issues.append({"kind": "env-config", "detail": f"environments.{args.env}.enabled=false"})
    analyzed = (
        analyze_required_secrets(args.env, entry)
        if mode == "docker"
        else {"secrets": list(entry.get("credential_refs", [])), "schema_errors": [], "warnings": []}
    )
    issues += [{"kind": "schema", "detail": item} for item in analyzed["schema_errors"]]
    secret_file = ensure_secrets_file(args.env, analyzed["secrets"])
    snapshot = load_secrets_file_snapshot(args.env, analyzed["secrets"])
    if secret_file["created"]:
        issues.append({"kind": "secret-file", "detail": f"已生成 {secret_file['path']}，请填写后重跑"})
    if snapshot["error"]:
        issues.append({"kind": "secret-file", "detail": snapshot["error"]})
    deployment_values = {**os.environ, **snapshot["values"]}
    validation_values = {
        **deployment_values,
        **load_local_runtime_env_snapshot(root)["values"],
    }
    validation = collect_validation_issues(deploy, validation_values, environment=args.env)
    issues += [{"kind": "schema", "detail": item} for item in validation.schema_errors] + [
        {"kind": "secret", "detail": item} for item in validation.secret_errors
    ]
    try:
        mode, deployment_asset, asset_issues = deployment_asset_check(args.env, entry, harness)
        issues += [{"kind": "deployment-asset", "detail": item} for item in asset_issues]
    except ValueError as exc:
        mode, deployment_asset = "unknown", Path(entry.get("compose_file", ""))
        issues.append({"kind": "deploy-mode", "detail": str(exc)})
    input_identity, identity_errors = promotion_input_identity(
        root,
        args.env,
        deploy,
        harness,
        entry,
        deployment_values,
    )
    issues += [{"kind": "deployment-identity", "detail": item} for item in identity_errors]
    state = {
        "schema_version": 1,
        "env": args.env,
        "enabled": bool(entry.get("enabled")),
        "deploy_mode": mode,
        "deployment_asset": str(deployment_asset),
        "required_secrets": analyzed["secrets"],
        "secrets_file": secret_file["path"],
        "health_url": entry.get("health_url"),
        "task_binding": task_binding,
        "input_identity": input_identity,
        "issues": issues,
        "ready": not issues,
        "ts": datetime.now(UTC).isoformat(),
    }
    store = StateStore(root / ".harness/state")
    target = store.write_json(f"promote-prep-{args.env}.json", state)
    if task_binding:
        target = store.write_json(promotion_preflight_filename(args.env, task_binding), state)
    for issue in issues:
        print(f"❌ [{issue['kind']}] {issue['detail']}")
    print(f"{'✅' if not issues else '❌'} promote-prep {args.env}: {target}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Attempt-scoped promotion Preflight identity and receipt validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mai_harness.runtime.application.required_secrets import analyze_required_secrets
from mai_harness.runtime.infrastructure.harness_config import assert_deploy_mode_implemented


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _expand(value: object, runtime_values: dict[str, str]) -> str:
    return re.sub(
        r"\$\{([A-Z0-9_]+)\}",
        lambda match: runtime_values.get(match.group(1), match.group(0)),
        str(value or ""),
    )


def _endpoint(value: str) -> dict[str, object]:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "port": port,
        "user": parsed.username or "",
        "path": parsed.path,
    }


def _asset_record(root: Path, raw_path: object) -> tuple[dict[str, str] | None, str | None]:
    value = Path(str(raw_path or ""))
    path = (root / value).resolve() if not value.is_absolute() else value.resolve()
    if not str(raw_path or "") or not path.is_relative_to(root.resolve()):
        return None, f"部署资产路径无效或越界: {raw_path}"
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        kind = "file"
    elif path.is_dir():
        files = sorted(item for item in path.rglob("*") if item.is_file())
        digest = _digest(
            [
                {
                    "path": item.relative_to(path).as_posix(),
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                }
                for item in files
            ]
        )
        kind = "directory"
    else:
        return None, f"部署资产不存在: {raw_path}"
    return {
        "path": path.relative_to(root.resolve()).as_posix(),
        "kind": kind,
        "sha256": digest,
    }, None


def credential_references(environment: str, entry: dict[str, Any], mode: str) -> list[str]:
    """Return credential names only; secret values never enter Preflight state."""

    if mode == "cloud-native":
        return sorted(str(item) for item in entry.get("credential_refs", []))
    analyzed = analyze_required_secrets(environment, entry)
    return sorted(str(item) for item in analyzed["secrets"])


def promotion_input_identity(
    root: Path,
    environment: str,
    deploy_config: dict[str, Any],
    harness_config: dict[str, Any],
    entry: dict[str, Any],
    runtime_values: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Digest the effective target, config and deployment assets used after Preflight."""

    mode = str(entry.get("deploy_mode") or harness_config["deploy"].get(f"{environment}_mode", "docker"))
    try:
        assert_deploy_mode_implemented(mode, environment)
    except ValueError as exc:
        return {}, [str(exc)]

    asset_paths: list[object] = []
    if mode == "cloud-native":
        asset_paths.append(f".harness/state/helm-plan-{environment}.yml")
        for release in entry.get("helm_releases", []):
            if isinstance(release, dict):
                asset_paths.append(release.get("chart", ""))
                asset_paths.extend(release.get("values_files", []))
        target = {
            "mode": mode,
            "context": str(entry.get("context", "")),
            "cluster_identity": str(entry.get("cluster_identity", "")),
            "namespace": str(entry.get("namespace", "")),
            "releases": [
                {"name": str(item.get("name", "")), "version": str(item.get("version", ""))}
                for item in entry.get("helm_releases", [])
                if isinstance(item, dict)
            ],
            "health_endpoint": _endpoint(_expand(entry.get("health_url"), runtime_values)),
        }
    else:
        compose_file = str(entry.get("compose_file", f"deploy/{environment}/docker-compose.yml"))
        asset_paths.append(compose_file)
        deploy_target = _expand(runtime_values.get("HARNESS_DEPLOY_TARGET", entry.get("deploy_target")), runtime_values)
        remote_workdir = _expand(
            runtime_values.get("HARNESS_REMOTE_WORKDIR", entry.get("remote_workdir")), runtime_values
        )
        target = {
            "mode": mode,
            "endpoint": _endpoint(deploy_target),
            "remote_workdir": remote_workdir,
            "compose_file": compose_file,
            "remote_compose_file": str(entry.get("remote_compose_file", compose_file)),
            "health_endpoint": _endpoint(_expand(entry.get("health_url"), runtime_values)),
        }

    adapter = entry.get("adapter")
    if isinstance(adapter, dict):
        argv = adapter.get("argv", [])
        target["adapter_argv"] = list(argv) if isinstance(argv, list) else []
        for token in argv if isinstance(argv, list) else []:
            candidate = root / token
            if candidate.exists():
                asset_paths.append(token)

    assets: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for asset_path in asset_paths:
        if not asset_path or str(asset_path) in seen:
            continue
        seen.add(str(asset_path))
        record, error = _asset_record(root, asset_path)
        if error:
            errors.append(error)
        elif record:
            assets.append(record)

    effective_target = (
        {
            "target": deploy_target,
            "remote_workdir": remote_workdir,
            "health_url": _expand(entry.get("health_url"), runtime_values),
        }
        if mode == "docker"
        else {
            "context": entry.get("context", ""),
            "cluster_identity": entry.get("cluster_identity", ""),
            "namespace": entry.get("namespace", ""),
            "health_url": _expand(entry.get("health_url"), runtime_values),
        }
    )
    identity = {
        "schema_version": 1,
        "environment": environment,
        "config_sha256": _digest(
            {
                "version": deploy_config.get("version"),
                "build": deploy_config.get("build"),
                "environment": entry,
                "harness_deploy": harness_config.get("deploy", {}),
            }
        ),
        "effective_target_sha256": _digest(effective_target),
        "target": target,
        "credential_refs": credential_references(environment, entry, mode),
        "assets": sorted(assets, key=lambda item: item["path"]),
    }
    return identity, errors


def promotion_preflight_filename(environment: str, task_binding: dict[str, Any]) -> str:
    components = (
        environment,
        task_binding["sprint"],
        task_binding["task_id"],
        task_binding["attempt_run_id"],
    )
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", str(item)) for item in components):
        raise ValueError("Preflight 回执路径只能包含字母、数字、点、下划线和连字符")
    return (
        f"promote-prep/{environment}/{task_binding['sprint']}/{task_binding['task_id']}/"
        f"{task_binding['attempt_run_id']}.json"
    )


def validate_promotion_preflight(
    root: Path,
    environment: str,
    task_binding: dict[str, Any],
    *,
    expected_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and bind the run-scoped Preflight receipt for one promote attempt."""

    relative = promotion_preflight_filename(environment, task_binding)
    path = root / ".harness/state" / relative
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"promote-test 无法读取当前 attempt 的 Preflight 回执: {path}: {exc}"]
    errors: list[str] = []
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("env") != environment
        or receipt.get("task_binding") != task_binding
        or receipt.get("ready") is not True
    ):
        errors.append("promote-test Preflight 回执未绑定当前 task attempt 或未通过")
    if expected_identity is not None and (
        not isinstance(receipt, dict) or receipt.get("input_identity") != expected_identity
    ):
        errors.append("promote-test 部署配置、目标、凭据引用或资产已在 Preflight 后漂移")
    binding = {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
        "task_binding": task_binding,
    }
    return binding, errors

"""Load config/deploy.yml with the legacy two-file compatibility path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import load_yaml, warn

DEPLOY_PATH = Path(os.environ.get("DEPLOY_CONFIG_PATH", str(PATHS.project_config / "deploy.yml")))
LEGACY_ENV_PATH = Path("config/environments.yml")
LEGACY_BUILD_PATH = Path("config/build-targets.yml")
_cache: dict[str, Any] | None = None


def merge_legacy_config(env_config: dict[str, Any], build_config: dict[str, Any] | None) -> dict[str, Any]:
    try:
        harness = load_harness_config()
    except Exception:
        harness = {}
    output: dict[str, Any] = {
        "version": env_config.get("version", 1),
        "promote_strategy": env_config.get("promote_strategy", "manual"),
        "train_schedule": env_config.get("train_schedule", "0 17 * * 1-5"),
        "build": None,
        "environments": {},
    }
    if build_config:
        output["build"] = {
            "image_repo": build_config.get("image_repo"),
            "default_platform": build_config.get("default_platform", "linux/amd64"),
            "artifact_dir": build_config.get("artifact_dir", ".harness/images"),
            "targets": build_config.get("targets", {}),
        }
    for name, entry in env_config.get("environments", {}).items():
        output["environments"][name] = {
            **entry,
            "deploy_mode": entry.get("deploy_mode") or harness.get("deploy", {}).get(f"{name}_mode", "docker"),
        }
    return output


def load_deploy_config(*, force: bool = False, path: Path | None = None) -> dict[str, Any]:
    global _cache
    deploy_path = path or DEPLOY_PATH
    legacy_env_path = deploy_path.parent / "environments.yml"
    legacy_build_path = deploy_path.parent / "build-targets.yml"
    if path is None and _cache is not None and not force:
        return _cache
    if deploy_path.exists():
        loaded = load_yaml(deploy_path)
    elif legacy_env_path.exists():
        warn(f"{deploy_path} 不存在，回退到 legacy 配置；建议迁移到 config/deploy.yml")
        loaded = merge_legacy_config(
            load_yaml(legacy_env_path), load_yaml(legacy_build_path) if legacy_build_path.exists() else None
        )
    else:
        raise FileNotFoundError(f"部署配置缺失：未找到 {deploy_path}（亦无 {legacy_env_path} 可回退）")
    if path is None:
        _cache = loaded
    return loaded


def get_environment(name: str) -> dict[str, Any]:
    entry = load_deploy_config().get("environments", {}).get(name)
    if not entry:
        raise KeyError(f"config/deploy.yml 中未声明 environments.{name}")
    return entry


def load_environments_compat() -> dict[str, Any]:
    config = load_deploy_config()
    return {key: config.get(key) for key in ("version", "promote_strategy", "train_schedule", "environments")}


def load_build_targets_compat() -> dict[str, Any]:
    return load_deploy_config().get("build") or {}


def reset_deploy_config_cache() -> None:
    global _cache
    _cache = None

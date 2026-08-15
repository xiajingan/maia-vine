"""Load and validate the project-level Harness configuration."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.modes import validate_mode_config
from mai_harness.runtime.infrastructure.core.command import harness_command
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import load_yaml

SOURCE_CONFIG = PATHS.runtime / "config"
DEFAULT_CONFIG_PATH = (
    PATHS.framework_config / "harness.defaults.yml"
    if PATHS.framework_config.exists()
    else SOURCE_CONFIG / "harness.defaults.yml"
)
HARNESS_CONFIG_PATH = PATHS.project_config / "harness.yml"
HARNESS_DEFAULTS: dict[str, Any] = load_yaml(DEFAULT_CONFIG_PATH)

SCHEMA = {
    "agent_runtime.primary": (str, {"codex", "agy", "copilot"}),
    "project.mode": (str, {"standalone", "managed", "control"}),
    "project.stack": (str, {"python-backend", "fullstack", "frontend"}),
    "automation.enabled": (bool, None),
    "automation.default_mode": (str, {"report-only", "safe-fix"}),
    "walkthrough_env": (str, {"development", "test"}),
    "gates.ui_design_l3": (bool, None),
    "gates.quality_threshold": (int, range(1, 101)),
    "gates.require_e2e": (bool, None),
    "deploy.test_mode": (str, {"docker", "cloud-native", "native"}),
    "deploy.prod_mode": (str, {"docker", "cloud-native", "native"}),
}
_cache: dict[str, Any] | None = None


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        result[key] = (
            deep_merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
        )
    return result


def _get(config: dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for key in dotted.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def validate(config: dict[str, Any]) -> list[str]:
    errors: list[str] = validate_mode_config(config)
    for path, (kind, allowed) in SCHEMA.items():
        value = _get(config, path)
        if value is None:
            errors.append(f"{path}: 缺失")
        elif type(value) is not kind:  # bool must not pass as int
            errors.append(f"{path}: 应为 {kind.__name__}")
        elif allowed is not None and value not in allowed:
            errors.append(f"{path}: 非法值 {value!r}")
    for name, command in config.get("commands", {}).items():
        if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
            errors.append(f"commands.{name}: 必须是非空字符串组成的 argv 数组或空数组")
    command_names = set(config.get("commands", {}))
    conditions = config.get("command_conditions", {})
    if not isinstance(conditions, dict):
        errors.append("command_conditions: 必须是对象")
        conditions = {}
    for name, condition in conditions.items():
        if name not in command_names:
            errors.append(f"command_conditions.{name}: 未定义对应命令")
        if not isinstance(condition, dict) or set(condition) - {"file_exists", "package_dependencies", "package_scripts"}:
            errors.append(f"command_conditions.{name}: 仅允许 file_exists/package_dependencies/package_scripts")
            continue
        if not isinstance(condition.get("file_exists"), str) or not condition["file_exists"]:
            errors.append(f"command_conditions.{name}.file_exists: 必须是非空相对路径")
        elif Path(condition["file_exists"]).is_absolute() or ".." in Path(condition["file_exists"]).parts:
            errors.append(f"command_conditions.{name}.file_exists: 必须是安全的工程内相对路径")
        for key in ("package_dependencies", "package_scripts"):
            values = condition.get(key, [])
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                errors.append(f"command_conditions.{name}.{key}: 必须是非空字符串数组")
    for name, members in config.get("command_groups", {}).items():
        if not isinstance(members, list) or not all(isinstance(item, str) for item in members):
            errors.append(f"command_groups.{name}: 必须是命令名称数组")
        elif unknown := set(members) - command_names:
            errors.append(f"command_groups.{name}: 未定义命令 {sorted(unknown)}")
    for name, job in config.get("automation", {}).get("jobs", {}).items():
        if not isinstance(job, dict) or not (job.get("command") or job.get("internal")):
            errors.append(f"automation.jobs.{name}: command/internal 至少声明一个")
        if job.get("command") and not isinstance(job["command"], list):
            errors.append(f"automation.jobs.{name}.command: 必须是 argv 数组")
    for environment, patterns in config.get("delivery", {}).get("branches", {}).items():
        if not isinstance(patterns, list) or not patterns:
            errors.append(f"delivery.branches.{environment}: 必须是非空正则数组")
            continue
        for pattern in patterns:
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                errors.append(f"delivery.branches.{environment}: 非法正则 {pattern!r}")
    if config.get("project", {}).get("mode") == "control":
        environments = config.get("control", {}).get("kubernetes", {}).get("environments", {})
        for environment in ("test", "prod"):
            policy = environments.get(environment, {})
            for field in ("context", "cluster", "namespace"):
                if not isinstance(policy.get(field), str):
                    errors.append(f"control.kubernetes.environments.{environment}.{field}: 缺失")
        for name in ("integration_commands", "production_verification_commands", "supply_chain_verification_commands"):
            commands = config.get("control", {}).get(name, [])
            if not isinstance(commands, list) or any(
                not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command)
                for command in commands
            ):
                errors.append(f"control.{name}: 必须是 argv 数组列表")
            if name == "supply_chain_verification_commands" and any(
                not any("{manifest}" in argument for argument in command) for command in commands
            ):
                errors.append("control.supply_chain_verification_commands: 每条命令必须显式包含 {manifest}")
    return errors


def load_harness_config(
    *, force: bool = False, path: Path | None = None, defaults_path: Path | None = None
) -> dict[str, Any]:
    global _cache
    use_cache = path is None and defaults_path is None
    if use_cache and _cache is not None and not force:
        return _cache
    source = path or HARNESS_CONFIG_PATH
    baseline = defaults_path or DEFAULT_CONFIG_PATH
    defaults = load_yaml(baseline)
    if not defaults:
        raise FileNotFoundError(f"Harness 默认配置不存在: {baseline}")
    user = load_yaml(source) if source.exists() else {}
    project = user.setdefault("project", {}) if user else {}
    if "profile" in project and "stack" not in project:
        project["stack"] = project.pop("profile")
    merged = deep_merge(defaults, user)
    weights = [item.get("weight") for item in merged.get("quality", {}).get("dimensions", {}).values()]
    if any(type(item) is not int or item < 0 for item in weights) or sum(weights) != 100:
        raise ValueError("harness.yml: quality.dimensions 权重必须为非负整数且总和等于 100")
    errors = validate(merged)
    if errors:
        raise ValueError("harness.yml 校验失败:\n  - " + "\n  - ".join(errors))
    merged["_source"] = str(source) if source.exists() else str(baseline)
    if use_cache:
        _cache = merged
    return merged


def assert_deploy_mode_implemented(mode: str, env: str) -> None:
    if mode in {"docker", "cloud-native"}:
        return
    raise ValueError(f"未知部署模式: {mode}")


def resolve_command(command: list[str]) -> list[str]:
    if command and command[0] == "{harness}":
        return harness_command(*command[1:])
    resolved = []
    for item in command:
        if item == "{python}":
            resolved.append(sys.executable)
        else:
            resolved.append(item)
    return resolved


def load_package_document(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Load package.json once with encoding, top-level and field-value validation."""
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}, ["package.json 不存在"]
    except (json.JSONDecodeError, UnicodeError):
        return {}, ["package.json 无法解析"]
    if not isinstance(package, dict):
        return {}, ["package.json 顶层必须是对象"]
    errors = []
    for key in ("dependencies", "devDependencies", "scripts"):
        value = package.get(key, {})
        if not isinstance(value, dict):
            errors.append(f"package.json.{key} 必须是对象")
            package[key] = {}
        elif any(not isinstance(name, str) or not isinstance(item, str) or not item for name, item in value.items()):
            errors.append(f"package.json.{key} 的名称和内容必须是非空字符串")
    return package, errors


def inspect_package_capabilities(path: Path) -> tuple[set[str], set[str], list[str]]:
    """Return declared Node dependencies/scripts and structural diagnostics."""
    package, errors = load_package_document(path)
    dependencies = set(package.get("dependencies", {})) | set(package.get("devDependencies", {}))
    return dependencies, set(package.get("scripts", {})), errors


def command_enabled(config: dict[str, Any], name: str, root: Path | None = None) -> bool:
    condition = config.get("command_conditions", {}).get(name, {})
    if not isinstance(condition, dict):
        return False
    project = root or PATHS.project
    required_file = condition.get("file_exists")
    if required_file and not (project / required_file).is_file():
        return False
    dependencies = condition.get("package_dependencies", [])
    scripts = condition.get("package_scripts", [])
    if dependencies or scripts:
        declared, declared_scripts, errors = inspect_package_capabilities(project / "package.json")
        if errors or not set(dependencies) <= declared or not set(scripts) <= declared_scripts:
            return False
    return True


def resolve_command_group(
    config: dict[str, Any], name: str, root: Path | None = None, *, require_conditions: bool = False
) -> list[list[str]]:
    """Resolve the configured, ordered commands in a named execution group."""
    commands = config.get("commands", {})
    resolved = []
    for item in config.get("command_groups", {}).get(name, []):
        if not commands.get(item):
            continue
        if not command_enabled(config, item, root):
            if require_conditions:
                raise ValueError(f"commands.{item} 条件未满足，命令组 {name} 禁止跳过")
            continue
        resolved.append(resolve_command(commands[item]))
    return resolved

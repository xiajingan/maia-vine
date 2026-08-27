"""Load and validate the project-level Harness configuration."""

from __future__ import annotations

import copy
import json
import math
import re
import sys
import warnings
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.actions import QUALITY_ACTION_RESERVE_SECONDS, QUALITY_ACTION_TIMEOUT_SECONDS
from mai_harness.runtime.domain.modes import LEGACY_PROJECT_TYPES, PROJECT_TYPES, validate_mode_config
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
    "project.type": (str, PROJECT_TYPES),
    "automation.enabled": (bool, None),
    "automation.default_mode": (str, {"report-only", "safe-fix"}),
    "task_execution.max_review_retries": (int, range(0, 6)),
    "delivery.remote": (str, None),
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
    quality = config.get("quality", {})
    runtime = quality.get("runtime", {}) if isinstance(quality, dict) else None
    runtime_fields = {
        "command",
        "cleanup_command",
        "environment_handoff",
        "startup_timeout_seconds",
        "shutdown_timeout_seconds",
    }
    if not isinstance(runtime, dict):
        errors.append("quality.runtime: 必须是对象")
        runtime = {}
    elif unknown := set(runtime) - runtime_fields:
        errors.append(f"quality.runtime: 未知字段 {sorted(unknown)}")
    runtime_command = runtime.get("command", "")
    cleanup_command = runtime.get("cleanup_command", "")
    for field, value in (("command", runtime_command), ("cleanup_command", cleanup_command)):
        if not isinstance(value, str):
            errors.append(f"quality.runtime.{field}: 必须是命令名称或空字符串")
        elif value and not config.get("commands", {}).get(value):
            errors.append(f"quality.runtime.{field}: 必须引用已定义的非空命令")
    if cleanup_command and not runtime_command:
        errors.append("quality.runtime.cleanup_command: 只能在启用 runtime.command 时配置")
    if not isinstance(runtime.get("environment_handoff", False), bool):
        errors.append("quality.runtime.environment_handoff: 必须是 boolean")
    for field in ("startup_timeout_seconds", "shutdown_timeout_seconds"):
        value = runtime.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
            or value > QUALITY_ACTION_TIMEOUT_SECONDS
        ):
            errors.append(f"quality.runtime.{field}: 必须是有限、正数且不超过质量 Action 上限")
    action_evidence = quality.get("action_evidence", {}) if isinstance(quality, dict) else None
    if not isinstance(action_evidence, dict):
        errors.append("quality.action_evidence: 必须是对象")
        action_evidence = {}
    elif unknown := set(action_evidence) - {"command", "artifact"}:
        errors.append(f"quality.action_evidence: 未知字段 {sorted(unknown)}")
    evidence_command = action_evidence.get("command", "")
    evidence_artifact = action_evidence.get("artifact", "")
    if not isinstance(evidence_command, str):
        errors.append("quality.action_evidence.command: 必须是命令名称或空字符串")
    elif evidence_command and not config.get("commands", {}).get(evidence_command):
        errors.append("quality.action_evidence.command: 必须引用已定义的非空命令")
    artifact_path = Path(evidence_artifact) if isinstance(evidence_artifact, str) else Path()
    if not isinstance(evidence_artifact, str):
        errors.append("quality.action_evidence.artifact: 必须是字符串")
    elif evidence_artifact and (
        artifact_path.is_absolute() or ".." in artifact_path.parts or artifact_path.suffix != ".json"
    ):
        errors.append("quality.action_evidence.artifact: 必须是安全的工程内 JSON 路径")
    if bool(evidence_command) != bool(evidence_artifact):
        errors.append("quality.action_evidence: 启用时 command/artifact 必须同时配置")
    performance = config.get("quality", {}).get("performance_evidence", {})
    if not isinstance(performance, dict):
        errors.append("quality.performance_evidence: 必须是对象")
        performance = {}
    performance_command = performance.get("command", "")
    if not isinstance(performance_command, str):
        errors.append("quality.performance_evidence.command: 必须是命令名称或空字符串")
    elif performance_command and not config.get("commands", {}).get(performance_command):
        errors.append("quality.performance_evidence.command: 必须引用已定义的非空命令")
    for field in ("artifact", "artifact_type"):
        value = performance.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"quality.performance_evidence.{field}: 必须是非空字符串")
    test_node = performance.get("test_node")
    if not isinstance(test_node, str):
        errors.append("quality.performance_evidence.test_node: 必须是字符串")
    artifact = Path(str(performance.get("artifact", "")))
    if artifact.is_absolute() or ".." in artifact.parts or artifact.suffix != ".json":
        errors.append("quality.performance_evidence.artifact: 必须是安全的工程内 JSON 路径")
    for field in ("identity_paths", "count_fields", "zero_fields", "categories"):
        values = performance.get(field)
        empty_allowed = field == "categories" and not performance_command
        if (
            not isinstance(values, list)
            or (not values and not empty_allowed)
            or not all(isinstance(item, str) and item for item in values)
        ):
            errors.append(f"quality.performance_evidence.{field}: 必须是非空字符串数组")
        elif field == "identity_paths" and any(Path(item).is_absolute() or ".." in Path(item).parts for item in values):
            errors.append("quality.performance_evidence.identity_paths: 只允许安全的工程内路径")
        elif len(values) != len(set(values)):
            errors.append(f"quality.performance_evidence.{field}: 禁止重复值")
    count_fields = performance.get("count_fields", [])
    zero_fields = performance.get("zero_fields", [])
    if isinstance(count_fields, list) and isinstance(zero_fields, list) and set(count_fields) & set(zero_fields):
        errors.append("quality.performance_evidence: count_fields/zero_fields 禁止重叠")
    for field in ("min_elapsed_seconds", "duration_tolerance_seconds", "timeout_seconds", "target_concurrency"):
        value = performance.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            errors.append(f"quality.performance_evidence.{field}: 必须是正数")
    max_p99 = performance.get("max_p99_seconds")
    if isinstance(max_p99, bool) or not isinstance(max_p99, int | float) or max_p99 <= 0:
        errors.append("quality.performance_evidence.max_p99_seconds: 必须是正数")
    timeout = performance.get("timeout_seconds")
    minimum = performance.get("min_elapsed_seconds")
    if isinstance(timeout, int | float) and isinstance(minimum, int | float) and timeout <= minimum:
        errors.append("quality.performance_evidence.timeout_seconds: 必须大于 min_elapsed_seconds")
    if isinstance(timeout, int | float) and timeout + QUALITY_ACTION_RESERVE_SECONDS > QUALITY_ACTION_TIMEOUT_SECONDS:
        errors.append(
            "quality.performance_evidence.timeout_seconds: 必须为其他质量维度预留至少 "
            f"{QUALITY_ACTION_RESERVE_SECONDS} 秒"
        )
    if performance_command and (not performance.get("test_node") or not performance.get("categories")):
        errors.append("quality.performance_evidence: 启用 producer 时 test_node/categories 不得为空")
    command_names = set(config.get("commands", {}))
    conditions = config.get("command_conditions", {})
    if not isinstance(conditions, dict):
        errors.append("command_conditions: 必须是对象")
        conditions = {}
    for name, condition in conditions.items():
        if name not in command_names:
            errors.append(f"command_conditions.{name}: 未定义对应命令")
        if not isinstance(condition, dict) or set(condition) - {
            "file_exists",
            "package_dependencies",
            "package_scripts",
        }:
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
    delivery = config.get("delivery", {})
    manifests_dir = delivery.get("manifests_dir")
    if (
        not isinstance(manifests_dir, str)
        or not manifests_dir
        or Path(manifests_dir).is_absolute()
        or ".." in Path(manifests_dir).parts
    ):
        errors.append("delivery.manifests_dir: 必须是安全的工程内相对路径")
    library_verifiers = delivery.get("supply_chain_verification_commands", [])
    if not isinstance(library_verifiers, list) or any(
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
        or not any("{manifest}" in item for item in command)
        for command in library_verifiers
    ):
        errors.append("delivery.supply_chain_verification_commands: 必须是包含 {manifest} 的 argv 数组列表")
    dependencies = config.get("dependencies", {})
    if not isinstance(dependencies, dict):
        errors.append("dependencies: 必须是对象")
    else:
        providers = dependencies.get("providers", {})
        if not isinstance(providers, dict):
            errors.append("dependencies.providers: 必须是对象")
        else:
            for provider_id, provider in providers.items():
                if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", provider_id):
                    errors.append(f"dependencies.providers.{provider_id}: provider ID 非法")
                    continue
                if not isinstance(provider, dict):
                    errors.append(f"dependencies.providers.{provider_id}: 必须是对象")
                    continue
                if "kind" in provider:
                    errors.append(
                        f"dependencies.providers.{provider_id}.kind: 已移除；Provider 工程自身必须使用 type=library"
                    )
                if provider.get("orchestration") not in {"coordinated", "assignment"}:
                    errors.append(
                        f"dependencies.providers.{provider_id}.orchestration: 必须为 coordinated 或 assignment"
                    )
                for field in ("path", "package"):
                    if not isinstance(provider.get(field), str) or not provider[field].strip():
                        errors.append(f"dependencies.providers.{provider_id}.{field}: 必须是非空字符串")
                capabilities = provider.get("capabilities", {})
                if not isinstance(capabilities, dict):
                    errors.append(f"dependencies.providers.{provider_id}.capabilities: 必须是对象")
                    continue
                for capability_id, capability in capabilities.items():
                    if not isinstance(capability_id, str) or not re.fullmatch(
                        r"[a-z0-9][a-z0-9._-]{2,127}", capability_id
                    ):
                        errors.append(
                            f"dependencies.providers.{provider_id}.capabilities.{capability_id}: capability ID 非法"
                        )
                    if not isinstance(capability, dict):
                        errors.append(f"dependencies.providers.{provider_id}.capabilities.{capability_id}: 必须是对象")
                        continue
                    commands = capability.get("consumer_contract_commands", [])
                    if (
                        not isinstance(commands, list)
                        or not commands
                        or not all(isinstance(item, str) and item for item in commands)
                    ):
                        errors.append(
                            f"dependencies.providers.{provider_id}.capabilities.{capability_id}.consumer_contract_commands: 必须是非空命令名称数组"
                        )
                    elif unknown := {name for name in commands if not config.get("commands", {}).get(name)}:
                        errors.append(
                            f"dependencies.providers.{provider_id}.capabilities.{capability_id}: 未定义命令 {sorted(unknown)}"
                        )
                    lock_command = capability.get("consumer_lock_command")
                    if lock_command is not None and (
                        not isinstance(lock_command, str) or not config.get("commands", {}).get(lock_command)
                    ):
                        errors.append(
                            f"dependencies.providers.{provider_id}.capabilities.{capability_id}.consumer_lock_command: 必须引用已定义命令"
                        )
    project = config.get("project", {})
    if isinstance(project, dict) and project.get("mode") == "control":
        environments = config.get("control", {}).get("kubernetes", {}).get("environments", {})
        for environment in ("test", "prod"):
            policy = environments.get(environment, {})
            for field in ("context", "cluster", "namespace"):
                if not isinstance(policy.get(field), str):
                    errors.append(f"control.kubernetes.environments.{environment}.{field}: 缺失")
        for name in ("integration_commands", "production_verification_commands", "supply_chain_verification_commands"):
            commands = config.get("control", {}).get(name, [])
            if not isinstance(commands, list) or any(
                not isinstance(command, list)
                or not command
                or not all(isinstance(item, str) and item for item in command)
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
    if not isinstance(defaults, dict):
        raise ValueError(f"Harness 默认配置顶层必须是对象: {baseline}")
    user = load_yaml(source) if source.exists() else {}
    if not isinstance(user, dict):
        raise ValueError(f"Harness 项目配置顶层必须是对象: {source}")
    project = user.get("project")
    if isinstance(project, dict):
        legacy_fields = [field for field in ("stack", "profile") if field in project]
        if "type" not in project and len(legacy_fields) == 1:
            legacy = legacy_fields[0]
            raw_value = project.pop(legacy)
            value = LEGACY_PROJECT_TYPES.get(str(raw_value), raw_value)
            project["type"] = value
            if value in LEGACY_PROJECT_TYPES.values():
                warnings.warn(
                    f"project.{legacy} 已废弃；请迁移为 project.type={value}",
                    FutureWarning,
                    stacklevel=2,
                )
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
    return not command_diagnostics(config, name, root)


def command_diagnostics(config: dict[str, Any], name: str, root: Path | None = None) -> list[str]:
    condition = config.get("command_conditions", {}).get(name, {})
    if not isinstance(condition, dict):
        return [f"commands.{name}: command_conditions 必须是对象"]
    project = root or PATHS.project
    required_file = condition.get("file_exists")
    if required_file and not (project / required_file).is_file():
        return [f"commands.{name}: 缺少 {required_file}"]
    dependencies = condition.get("package_dependencies", [])
    scripts = condition.get("package_scripts", [])
    if dependencies or scripts:
        declared, declared_scripts, errors = inspect_package_capabilities(project / "package.json")
        if errors:
            return [f"commands.{name}: {error}" for error in errors]
        missing_dependencies = set(dependencies) - declared
        missing_scripts = set(scripts) - declared_scripts
        diagnostics = []
        if missing_dependencies:
            diagnostics.append(f"commands.{name}: 缺少依赖 {', '.join(sorted(missing_dependencies))}")
        if missing_scripts:
            diagnostics.append(f"commands.{name}: 缺少 scripts.{', scripts.'.join(sorted(missing_scripts))}")
        return diagnostics
    return []


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

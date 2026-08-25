"""Load the project technology declaration and validate executable capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.modes import PROJECT_TYPE_COMPONENTS
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import command_diagnostics, deep_merge
from mai_harness.runtime.infrastructure.utils import load_yaml

SOURCE_CONFIG = PATHS.runtime / "config"
DEFAULT_TECHNOLOGY_PATH = (
    PATHS.framework_config / "technology.defaults.yml"
    if PATHS.framework_config.exists()
    else SOURCE_CONFIG / "technology.defaults.yml"
)
TECHNOLOGY_CONFIG_PATH = PATHS.project_config / "technology.yml"


def _safe_relative(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def validate_technology(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("version") != 1:
        errors.append("version: 必须为 1")
    components = config.get("components")
    if not isinstance(components, dict) or set(components) != {"backend", "frontend", "library"}:
        return [*errors, "components: 必须完整声明 backend/frontend/library"]
    for name, component in components.items():
        prefix = f"components.{name}"
        if not isinstance(component, dict):
            errors.append(f"{prefix}: 必须是对象")
            continue
        for field in ("language", "framework"):
            if not isinstance(component.get(field), str) or not component[field].strip():
                errors.append(f"{prefix}.{field}: 必须是非空字符串")
        if not _safe_relative(component.get("manifest")):
            errors.append(f"{prefix}.manifest: 必须是安全的工程内相对路径")
        commands = component.get("required_commands")
        if (
            not isinstance(commands, list)
            or not commands
            or len(commands) != len(set(commands))
            or not all(isinstance(item, str) and item for item in commands)
        ):
            errors.append(f"{prefix}.required_commands: 必须是无重复的非空命令名称数组")
            commands = []
        unit_command = component.get("unit_command")
        if not isinstance(unit_command, str) or unit_command not in commands:
            errors.append(f"{prefix}.unit_command: 必须引用 required_commands 中的单元测试命令")
        if name == "library" and not _safe_relative(component.get("artifact_glob")):
            errors.append(f"{prefix}.artifact_glob: 必须是安全的工程内相对 glob")
    return errors


def load_technology_config(*, path: Path | None = None, defaults_path: Path | None = None) -> dict[str, Any]:
    source = path or TECHNOLOGY_CONFIG_PATH
    baseline = defaults_path or DEFAULT_TECHNOLOGY_PATH
    defaults = load_yaml(baseline)
    if not isinstance(defaults, dict):
        raise ValueError(f"Technology 默认配置顶层必须是对象: {baseline}")
    project = load_yaml(source) if source.exists() else {}
    if not isinstance(project, dict):
        raise ValueError(f"Technology 项目配置顶层必须是对象: {source}")
    merged = deep_merge(defaults, project)
    if errors := validate_technology(merged):
        raise ValueError("technology.yml 校验失败:\n  - " + "\n  - ".join(errors))
    merged["_source"] = str(source) if source.exists() else str(baseline)
    return merged


def active_components(project_type: str) -> tuple[str, ...]:
    try:
        return PROJECT_TYPE_COMPONENTS[project_type]
    except KeyError as exc:
        raise ValueError(f"未知 project.type: {project_type}") from exc


def unit_command_names(technology: dict[str, Any], project_type: str) -> list[str]:
    return [technology["components"][name]["unit_command"] for name in active_components(project_type)]


def validate_technology_capabilities(technology: dict[str, Any], harness: dict[str, Any], root: Path) -> list[str]:
    errors: list[str] = []
    project_type = harness.get("project", {}).get("type")
    commands = harness.get("commands", {})
    for name in active_components(str(project_type)):
        component = technology["components"][name]
        manifest = root / component["manifest"]
        if not manifest.is_file():
            errors.append(
                f"technology.components.{name}.manifest 不存在: {component['manifest']} "
                f"（当前 {component['language']}/{component['framework']}）"
            )
        for command_name in component["required_commands"]:
            if not commands.get(command_name):
                errors.append(f"technology.components.{name}: 必需命令 commands.{command_name} 未配置")
            else:
                errors.extend(
                    f"technology.components.{name}: {diagnostic}"
                    for diagnostic in command_diagnostics(harness, command_name, root)
                )
    return errors

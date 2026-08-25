"""Shared project mode policy consumed by Bootstrap and installed runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODES = {"standalone", "managed", "control"}
PROJECT_TYPES = {"backend", "fullstack", "frontend", "library"}
PROJECT_TYPE_COMPONENTS = {
    "backend": ("backend",),
    "frontend": ("frontend",),
    "fullstack": ("backend", "frontend"),
    "library": ("library",),
}
LEGACY_PROJECT_TYPES = {
    "backend": "backend",
    "python-backend": "backend",
    "frontend": "frontend",
    "fullstack": "fullstack",
    "library": "library",
}


def resolve_project_type(project: dict[str, Any]) -> str | None:
    """Resolve the canonical project type while accepting one legacy field."""
    declared = [field for field in ("type", "stack", "profile") if field in project]
    if len(declared) != 1:
        return None
    field = declared[0]
    value = project.get(field)
    return value if field == "type" else LEGACY_PROJECT_TYPES.get(str(value))


@dataclass(frozen=True)
class ModePolicy:
    mode: str
    sprint: bool
    assignments: bool
    control: bool
    shared_deploy: bool
    system_e2e: bool


POLICIES = {
    "standalone": ModePolicy("standalone", True, False, False, True, True),
    "managed": ModePolicy("managed", True, True, False, False, False),
    "control": ModePolicy("control", True, False, True, True, True),
}


def validate_mode_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    project = config.get("project", {})
    if not isinstance(project, dict):
        return ["project: 必须是对象"]
    if "kind" in project:
        errors.append("project.kind: 已移除；公共包直接使用 project.type=library")
    declared_types = [field for field in ("type", "stack", "profile") if field in project]
    if len(declared_types) > 1:
        errors.append(f"project: type/stack/profile 只能声明一个，实际 {declared_types}")
    mode = project.get("mode")
    if mode not in MODES:
        return [f"project.mode: 必须是 {sorted(MODES)}"]
    project_type = resolve_project_type(project)
    if project_type not in PROJECT_TYPES:
        errors.append(f"project.type: 必须是 {sorted(PROJECT_TYPES)}")
    if mode == "control" and project_type == "library":
        errors.append("project.type: control 模式不能声明为 library")
    if not isinstance(project.get("id"), str) or not project["id"].strip():
        errors.append("project.id: 必须是稳定的非空工程 ID")
    expected_owner = "control" if mode in {"managed", "control"} else "self"
    if project.get("deployment_owner") != expected_owner:
        errors.append(f"project.deployment_owner: mode={mode} 时必须为 {expected_owner}")
    management = config.get("management", {})
    if not isinstance(management, dict):
        errors.append("management: 必须是对象")
        management = {}
    if mode == "managed":
        required = (
            "control_id",
            "control_path",
            "intake_policy",
            "assignment_inbox",
            "assignment_responses",
            "deliveries_dir",
        )
        errors.extend(f"management.{key}: managed 模式必填" for key in required if not management.get(key))
        if management.get("intake_policy") != "manual-planning":
            errors.append("management.intake_policy: 仅允许 manual-planning")
        for key in ("assignment_inbox", "assignment_responses", "deliveries_dir"):
            value = management.get(key)
            if value and (Path(value).is_absolute() or ".." in Path(value).parts):
                errors.append(f"management.{key}: 必须是工程内相对路径")
        verification_commands = management.get("supply_chain_verification_commands", [])
        if not isinstance(verification_commands, list) or any(
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
            or not any("{manifest}" in item for item in command)
            for command in verification_commands
        ):
            errors.append("management.supply_chain_verification_commands: 必须是包含 {manifest} 的 argv 数组列表")
        control_path = management.get("control_path")
        if control_path and Path(control_path).is_absolute():
            errors.append("management.control_path: 必须是相对 Managed 的路径")
    elif management:
        errors.append(f"management: 仅 managed 模式允许声明（当前 {mode}）")
    control = config.get("control", {})
    if not isinstance(control, dict):
        errors.append("control: 必须是对象")
        control = {}
    if mode == "control" and not control.get("managed_projects"):
        errors.append("control.managed_projects: control 模式必填")
    elif mode != "control" and control:
        errors.append(f"control: 仅 control 模式允许声明（当前 {mode}）")
    return errors

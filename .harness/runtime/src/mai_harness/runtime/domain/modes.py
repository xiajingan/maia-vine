"""Shared project mode policy consumed by Bootstrap and installed runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODES = {"standalone", "managed", "control"}
STACKS = {"python-backend", "fullstack", "frontend"}


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
    mode = project.get("mode")
    if mode not in MODES:
        return [f"project.mode: 必须是 {sorted(MODES)}"]
    if project.get("stack") not in STACKS:
        errors.append(f"project.stack: 必须是 {sorted(STACKS)}")
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

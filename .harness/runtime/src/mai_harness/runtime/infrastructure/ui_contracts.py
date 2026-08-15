"""Load declarative project UI audit contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import load_yaml


def load_contracts(path: Path = PATHS.rules / "ui-contracts.yml", sprint_id: str = "") -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"未找到 {path}；项目必须声明 UI 契约或 required: false")
    document = load_yaml(path)
    if not isinstance(document, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    required = document.get("required", True)
    entries = document.get("contracts", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: contracts 必须是数组")
    contracts = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: contract 必须是对象")
        sprints = entry.get("sprints") or ([entry["sprint"]] if entry.get("sprint") else [])
        if sprint_id and sprints and sprint_id not in sprints and f"sprint-{sprint_id}" not in sprints:
            continue
        contracts.append(entry)
    return {
        "required": required,
        "reason": document.get("reason", ""),
        "contracts": contracts,
        "mock_matrix": document.get("mock_matrix", []),
    }


def validate_contracts(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan.get("required"), bool):
        errors.append("required 必须为 boolean")
    if plan.get("required") is False and not str(plan.get("reason", "")).strip():
        errors.append("required=false 时 reason 必填")
    for index, contract in enumerate(plan.get("contracts", [])):
        prefix = f"contracts[{index}]"
        for key in ("name", "prototype", "live", "viewport", "checks"):
            if not contract.get(key):
                errors.append(f"{prefix}.{key} 缺失")
        for target in ("prototype", "live"):
            if isinstance(contract.get(target), dict) and not contract[target].get("path"):
                errors.append(f"{prefix}.{target}.path 缺失")
        if not isinstance(contract.get("checks", []), list):
            errors.append(f"{prefix}.checks 必须是数组")
        else:
            for check_index, check in enumerate(contract["checks"]):
                if not isinstance(check, dict) or not check.get("kind"):
                    errors.append(f"{prefix}.checks[{check_index}].kind 缺失")
    return errors

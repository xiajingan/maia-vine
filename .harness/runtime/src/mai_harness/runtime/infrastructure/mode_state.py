"""Engineering-mode invariants and controlled profile migration."""

from __future__ import annotations

import json
from pathlib import Path

from mai_harness.runtime.domain.modes import MODES as MODES
from mai_harness.runtime.domain.modes import POLICIES as POLICIES
from mai_harness.runtime.domain.modes import STACKS as STACKS
from mai_harness.runtime.domain.modes import validate_mode_config as validate_mode_config


def profile_marker_path(project: Path) -> Path:
    return project / ".harness/state/profile.json"


def read_installed_mode(project: Path) -> str | None:
    marker = profile_marker_path(project)
    if not marker.exists():
        return None
    value = json.loads(marker.read_text(encoding="utf-8")).get("mode")
    if value not in MODES:
        raise ValueError(f"非法安装模式标记: {value!r}")
    return value


def assert_mode_consistent(project: Path, configured: str, requested: str | None = None) -> None:
    installed = read_installed_mode(project)
    expected = requested or configured
    if expected not in MODES:
        raise ValueError(f"非法请求模式: {expected!r}")
    if configured != expected:
        raise ValueError(f"CLI mode={expected} 与 config project.mode={configured} 不一致")
    if installed and installed != expected:
        raise ValueError(f"已安装 mode={installed} 与请求 mode={expected} 不一致；必须执行受控迁移")

"""Resolve registry or artifact delivery mode."""

from __future__ import annotations

import os
from typing import Any

from mai_harness.runtime.infrastructure.utils import fatal, warn

VALID_MODES = {"registry", "artifact"}


def resolve_delivery_mode(entry: dict[str, Any] | None = None) -> str:
    entry = entry or {}
    current = entry.get("delivery_mode")
    legacy = entry.get("registry_mode")
    if legacy and not current:
        warn("[deprecated] registry_mode 已改名为 delivery_mode；请尽快迁移")
    if legacy and current and legacy != current:
        fatal(f"delivery_mode={current} vs registry_mode={legacy} 存在歧义")
    mode = str(os.environ.get("HARNESS_DELIVERY_MODE") or current or legacy or "artifact").lower()
    if mode not in VALID_MODES:
        fatal(f"未知 HARNESS_DELIVERY_MODE：{mode}（合法值：registry | artifact）")
    return mode


resolve_registry_mode = resolve_delivery_mode

"""Resolve configured ownership for cross-artifact scope conflicts."""

from __future__ import annotations

from typing import Any

RESPONSIBLE_SCOPES = ("story", "product", "design", "technical-design")


def resolve_scope_route(rules: dict[str, Any], sprint_type: str, scope: str) -> dict[str, Any]:
    """Return one validated owner or transfer route; missing routes fail closed."""
    if scope not in RESPONSIBLE_SCOPES:
        raise ValueError(f"未知责任域: {scope}")
    route = ((rules.get("scope_conflict_routes") or {}).get(sprint_type) or {}).get(scope)
    if not isinstance(route, dict) or len(route) != 1:
        raise ValueError(f"sprint_type={sprint_type} 未配置唯一的 {scope} 责任域路线")
    if "owner_tasks" in route:
        owners = route["owner_tasks"]
        if (
            not isinstance(owners, list)
            or not owners
            or any(not isinstance(value, str) or not value for value in owners)
            or len(owners) != len(set(owners))
        ):
            raise ValueError(f"sprint_type={sprint_type} 的 {scope} 路线包含非法 owner_tasks")
        return {"owner_tasks": tuple(owners)}
    target = route.get("transfer_to")
    if not isinstance(target, str) or not target or target == sprint_type:
        raise ValueError(f"sprint_type={sprint_type} 的 {scope} 路线包含非法 transfer_to")
    return {"transfer_to": target}

"""Explainable dependency ordering for managed projects."""

from __future__ import annotations

from typing import Any


def dependency_order(projects: dict[str, dict[str, Any]]) -> list[str]:
    incoming = {name: set(value.get("depends_on", [])) for name, value in projects.items()}
    for name, deps in incoming.items():
        if missing := deps - projects.keys():
            raise ValueError(f"工程 {name} 依赖未登记工程: {sorted(missing)}")
        if name in deps:
            raise ValueError(f"工程 {name} 不能依赖自身")
    order: list[str] = []
    ready = sorted(name for name, deps in incoming.items() if not deps)
    while ready:
        name = ready.pop(0)
        order.append(name)
        for candidate, deps in incoming.items():
            if name in deps:
                deps.remove(name)
                if not deps and candidate not in order and candidate not in ready:
                    ready.append(candidate)
                    ready.sort()
    if len(order) != len(projects):
        blocked = sorted(name for name, deps in incoming.items() if deps)
        raise ValueError(f"工程依赖存在循环: {blocked}")
    return order

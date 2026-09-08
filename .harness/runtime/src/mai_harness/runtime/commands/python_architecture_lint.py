#!/usr/bin/env python3
"""Validate Python layer imports using the declarative Harness architecture policy."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from mai_harness.runtime.infrastructure.harness_config import load_harness_config

COMMON = {
    "types": {"directories": ["types", "schemas", "contracts"], "allow": ["types", "config", "shared"]},
    "config": {"directories": ["config"], "allow": ["config", "shared"]},
    "shared": {"directories": ["shared", "common", "lib"], "allow": ["shared", "types", "config"]},
}
PROFILE_LAYERS = {
    "simple-layered": {
        "controller": {
            "directories": ["controllers", "api", "routes"],
            "allow": ["service", "types", "config", "shared"],
        },
        "service": {"directories": ["services"], "allow": ["repository", "types", "config", "shared"]},
        "repository": {"directories": ["repositories"], "allow": ["types", "config", "shared"]},
        **COMMON,
    },
    "domain-centric": {
        "adapter": {
            "directories": ["adapters", "controllers", "api", "routes"],
            "allow": ["application", "domain", "ports", "types", "config", "shared"],
        },
        "application": {
            "directories": ["application", "use_cases", "usecases"],
            "allow": ["domain", "ports", "types", "config", "shared"],
        },
        "domain": {"directories": ["domain"], "allow": ["domain", "types", "shared"]},
        "ports": {"directories": ["ports"], "allow": ["domain", "types", "shared"]},
        "infrastructure": {
            "directories": ["infrastructure", "persistence", "clients", "repositories"],
            "allow": ["domain", "ports", "types", "config", "shared"],
        },
        **COMMON,
    },
    "event-driven": {
        "transport": {
            "directories": ["producers", "consumers", "handlers", "api"],
            "allow": ["application", "domain", "contracts", "types", "config", "shared"],
        },
        "application": {
            "directories": ["application", "use_cases", "usecases"],
            "allow": ["domain", "contracts", "ports", "types", "config", "shared"],
        },
        "domain": {"directories": ["domain"], "allow": ["domain", "contracts", "types", "shared"]},
        "ports": {"directories": ["ports"], "allow": ["domain", "contracts", "types", "shared"]},
        "infrastructure": {
            "directories": ["infrastructure", "persistence", "clients"],
            "allow": ["domain", "contracts", "ports", "types", "config", "shared"],
        },
        "contracts": {"directories": ["contracts", "events"], "allow": ["contracts", "types", "shared"]},
        **COMMON,
    },
}


def resolve_policy(policy: dict) -> tuple[dict, list[str]]:
    profile = policy.get("profile", "custom")
    errors: list[str] = []
    if profile in PROFILE_LAYERS:
        layers = PROFILE_LAYERS[profile]
    elif profile == "custom":
        layers = policy.get("layers") or {}
        if not layers:
            errors.append("python_architecture custom Profile 缺少 layers")
    else:
        layers = {}
        errors.append(f"未知 Python Architecture Profile: {profile}")
    known = set(layers)
    for name, rule in layers.items():
        if not isinstance(rule, dict) or not rule.get("directories") or not isinstance(rule.get("allow"), list):
            errors.append(f"Python Architecture 层定义非法: {name}")
        elif unknown := set(rule["allow"]) - known:
            errors.append(f"Python Architecture 层 {name} 引用了未知层: {sorted(unknown)}")
    source_roots = policy.get("source_roots") or []
    if not isinstance(source_roots, list) or not source_roots:
        errors.append("python_architecture.source_roots 必须是非空数组")
    return {"profile": profile, "source_roots": source_roots, "layers": layers}, errors


def architecture_profile(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, ""
    match = re.search(r"(?m)^\*\*当前 Profile\*\*\s*[:：]\s*`?([^`\n]+)", path.read_text(encoding="utf-8"))
    if not match:
        return False, ""
    value = match.group(1).strip()
    return True, value if value in {*PROFILE_LAYERS, "custom"} else ""


def layer_for(parts: tuple[str, ...], layers: dict) -> str | None:
    for name, rule in layers.items():
        if any(directory in parts for directory in rule["directories"]):
            return name
    return None


def violations(root: Path, policy: dict) -> list[str]:
    resolved, errors = resolve_policy(policy)
    layers = resolved["layers"]
    declared, documented_profile = architecture_profile(root / "ARCHITECTURE.md")
    if (root / "ARCHITECTURE.md").is_file() and not declared:
        errors.append("ARCHITECTURE.md 缺少可解析的 **当前 Profile** 声明")
    elif declared and documented_profile != resolved["profile"]:
        errors.append(
            "ARCHITECTURE.md 当前 Profile 与 config/harness.yml 不一致: "
            f"documented={documented_profile or '未完成'}, configured={resolved['profile']}"
        )
    for source_root in resolved["source_roots"]:
        directory = root / source_root
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            current = layer_for(path.relative_to(directory).parts, layers)
            if not current:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                errors.append(f"{path}:{exc.lineno}: Python 语法错误: {exc.msg}")
                continue
            allowed = set(layers[current]["allow"]) | {current}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = [module] if module else []
                    names.extend(".".join(part for part in (module, alias.name) if part) for alias in node.names)
                else:
                    names = []
                imported_layers = {
                    imported
                    for name in names
                    if (imported := layer_for(tuple(name.split(".")), layers)) is not None
                }
                for imported in sorted(imported_layers):
                    if imported and imported not in allowed:
                        errors.append(
                            f"{path}:{node.lineno}: {current} 不允许导入 {imported}；允许 {', '.join(sorted(allowed))}"
                        )
    return errors


def main() -> int:
    config = load_harness_config()
    policy = {**config["python_architecture"], "profile": config["architecture"]["profile"]}
    errors = violations(Path.cwd(), policy)
    for error in errors:
        print(f"❌ {error}")
    print(f"{'✅' if not errors else '❌'} Python architecture: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

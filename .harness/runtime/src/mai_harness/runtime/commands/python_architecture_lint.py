#!/usr/bin/env python3
"""Validate Python layer imports using the declarative Harness architecture policy."""

from __future__ import annotations

import ast
from pathlib import Path

from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def layer_for(parts: tuple[str, ...], layers: dict) -> str | None:
    for name, rule in layers.items():
        if any(directory in parts for directory in rule["directories"]):
            return name
    return None


def violations(root: Path, policy: dict) -> list[str]:
    layers = policy["layers"]
    errors: list[str] = []
    for source_root in policy["source_roots"]:
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
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                for name in names:
                    imported = layer_for(tuple(name.split(".")), layers)
                    if imported and imported not in allowed:
                        errors.append(
                            f"{path}:{node.lineno}: {current} 不允许导入 {imported}；允许 {', '.join(sorted(allowed))}"
                        )
    return errors


def main() -> int:
    errors = violations(Path.cwd(), load_harness_config()["python_architecture"])
    for error in errors:
        print(f"❌ {error}")
    print(f"{'✅' if not errors else '❌'} Python architecture: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

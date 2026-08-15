#!/usr/bin/env python3
"""Validate Harness YAML test-case schema."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import load_yaml

REQUIRED = ("id", "title", "priority", "last_verified_in", "preconditions", "steps", "tags", "spec")


@dataclass
class CaseResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total: int = 0
    live: int = 0


def validate_case(doc: Any, name: str, root: Path) -> CaseResult:
    result = CaseResult(total=1)
    error, warning = result.errors.append, result.warnings.append
    if not isinstance(doc, dict):
        error(f"{name}: 顶层必须是对象")
        return result
    introduced = doc.get("introduced_in") or doc.get("sprint")
    for key in REQUIRED:
        if key not in doc or doc[key] is None or doc[key] == "":
            error(f"{name}: 缺少必填字段 {key}")
    if not introduced:
        error(f"{name}: 缺少必填字段 introduced_in")
    if not doc.get("last_modified_in"):
        warning(f"{name}: 缺少 last_modified_in")
    if "sprint" in doc:
        warning(f"{name}: sprint 字段为 legacy；请改为 introduced_in")
    if doc.get("priority") not in (None, "P0", "P1", "P2"):
        error(f"{name}: priority 非法 ({doc['priority']})")
    for key in ("preconditions", "tags"):
        if doc.get(key) is not None and not isinstance(doc[key], list):
            error(f"{name}: {key} 必须是数组")
    if doc.get("steps") is not None:
        if not isinstance(doc["steps"], list):
            error(f"{name}: steps 必须是数组")
        else:
            for index, step in enumerate(doc["steps"]):
                if not isinstance(step, dict):
                    error(f"{name}: steps[{index}] 必须是对象")
                    continue
                for key in ("action", "expected"):
                    if not step.get(key):
                        error(f"{name}: steps[{index}].{key} 缺失")
    if isinstance(doc.get("spec"), str) and not (root / doc["spec"]).exists():
        warning(f"{name}: spec 文件不存在 — {doc['spec']}")
    execution = doc.get("execution") or {}
    mode = execution.get("mode", "standard")
    if mode not in ("standard", "live"):
        error(f"{name}: execution.mode 非法 ({mode})")
    if mode == "live":
        result.live = 1
        if not execution.get("env") and not str(execution.get("mock_reason", "")).strip():
            error(f"{name}: execution.mode=live 须声明 execution.env 或 execution.mock_reason")
        for key, value in (execution.get("env") or {}).items():
            if (
                isinstance(value, str)
                and re.search(r"[A-Za-z0-9+/=]{16,}", value)
                and not re.match(r"^(0|1|true|false|yes|no)$", value, re.I)
            ):
                warning(f"{name}: execution.env.{key} 疑似包含敏感值")
    return result


def scan(directory: Path, root: Path, sprint: str = "") -> CaseResult:
    combined = CaseResult()
    for file in sorted((*directory.rglob("*.yml"), *directory.rglob("*.yaml"))):
        if file.name == "index.yml":
            continue
        try:
            doc = load_yaml(file)
        except Exception as exc:
            combined.errors.append(f"{file}: YAML 解析失败 — {exc}")
            combined.total += 1
            continue
        introduced = doc.get("introduced_in") or doc.get("sprint") if isinstance(doc, dict) else None
        modified = doc.get("last_modified_in") or introduced if isinstance(doc, dict) else None
        if sprint:
            expected = sprint if sprint.startswith("sprint-") else f"sprint-{sprint}"
            related = [
                f"sprint-{value}" if value and not str(value).startswith("sprint-") else str(value or "")
                for value in (introduced, modified, doc.get("last_verified_in") if isinstance(doc, dict) else None)
            ]
            if expected not in related:
                continue
        result = validate_case(doc, str(file.relative_to(root)) if file.is_relative_to(root) else str(file), root)
        combined.errors += result.errors
        combined.warnings += result.warnings
        combined.total += result.total
        combined.live += result.live
    return combined


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=PATHS.test_cases)
    parser.add_argument("--sprint", default="")
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()
    if not args.dir.exists():
        print(f"测试用例目录不存在: {args.dir} — 跳过校验")
        return 0
    result = scan(args.dir, Path.cwd(), args.sprint)
    for message in result.errors:
        print(f"❌ {message}")
    for message in result.warnings:
        print(f"⚠️  {message}")
    print(f"用例总数: {result.total}，其中 live: {result.live}；{len(result.errors)} 错误, {len(result.warnings)} 警告")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

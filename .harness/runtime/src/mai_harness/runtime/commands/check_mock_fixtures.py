#!/usr/bin/env python3
"""Validate declared mock response value matrices."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.ui_contracts import load_contracts


def extract_by_path(root: object, path: str) -> list[object]:
    cursor = [root]
    for part in path.split("."):
        wildcard = part.endswith("[*]")
        key = part[:-3] if wildcard else part
        following = []
        for current in cursor:
            if not isinstance(current, dict):
                continue
            value = current.get(key)
            if wildcard and isinstance(value, list):
                following.extend(value)
            elif not wildcard:
                following.append(value)
        cursor = following
    return cursor


def validate_entry(entry: dict, body: object) -> list[str]:
    missing_fields = [key for key in ("endpoint", "expect_field", "required_values") if not entry.get(key)]
    if missing_fields:
        return [f"矩阵声明缺字段：{', '.join(missing_fields)}"]
    actual = set(extract_by_path(body, entry["expect_field"]))
    missing = [value for value in entry["required_values"] if value not in actual]
    return [f"缺少值 {missing}（实际 {sorted(str(item) for item in actual)}）"] if missing else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--mock-base", default=os.environ.get("MOCK_SERVER_URL", "http://localhost:3001"))
    parser.add_argument("--file", type=Path, default=PATHS.rules / "ui-contracts.yml")
    args = parser.parse_args()
    try:
        matrix = load_contracts(args.file, args.sprint).get("mock_matrix", [])
    except (OSError, ValueError) as exc:
        print(f"❌ {exc}")
        return 1
    if not matrix:
        print("✅ 未声明 mock_matrix，跳过")
        return 0
    failures = 0
    for entry in matrix:
        endpoint = str(entry.get("endpoint", "")).replace("${mock_base}", args.mock_base.rstrip("/"))
        try:
            with urlopen(endpoint, timeout=10) as response:
                body = json.load(response)
            issues = validate_entry(entry, body)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            issues = [f"请求失败: {exc}"]
        for issue in issues:
            print(f"❌ {entry.get('label', endpoint)}: {issue}")
        failures += bool(issues)
    if not failures:
        print("✅ mock 矩阵全部覆盖")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

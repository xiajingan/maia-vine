#!/usr/bin/env python3
"""Enforce minimum evidence strength for every UI contract."""

import argparse
from collections import Counter
from pathlib import Path

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.ui_contracts import load_contracts, validate_contracts

MIN_TOTAL = 6


def strength_issues(contract: dict) -> list[str]:
    checks = contract.get("checks", [])
    counts = Counter(item.get("kind") for item in checks if isinstance(item, dict))
    issues = []
    if len(checks) < MIN_TOTAL:
        issues.append(f"总检查项 {len(checks)} < {MIN_TOTAL}")
    for kind, label in (("textList", "关键文本/顺序"), ("style", "关键 CSS 样式"), ("metric", "布局尺寸/位置")):
        if not counts[kind]:
            issues.append(f"缺少 {kind}（{label}）")
    if not counts["presence"] and not counts["count"]:
        issues.append("缺少 presence 或 count（结构存在/数量）")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--file", type=Path, default=PATHS.rules / "ui-contracts.yml")
    args = parser.parse_args()
    try:
        plan = load_contracts(args.file, args.sprint)
    except (OSError, ValueError) as exc:
        print(f"❌ {exc}")
        return 1
    errors = validate_contracts(plan)
    if not plan["required"]:
        print(f"✅ UI 审计 required=false（{plan['reason']}），跳过")
        return 0 if not errors else 1
    if not plan["contracts"]:
        errors.append(f"Sprint {args.sprint} 未声明任何 contract")
    for contract in plan["contracts"]:
        for issue in strength_issues(contract):
            errors.append(f"{contract.get('name', 'unnamed')}: {issue}")
    for error in errors:
        print(f"❌ {error}")
    if not errors:
        print(f"✅ 全部 {len(plan['contracts'])} 个 contract 强度达标")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

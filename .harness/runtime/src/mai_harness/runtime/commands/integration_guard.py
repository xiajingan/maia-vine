#!/usr/bin/env python3
"""Validate the structured receipt emitted by one integration task."""

from __future__ import annotations

import argparse
from pathlib import Path

from mai_harness.runtime.application.integration_contract import validate_integration_receipt
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    sprint = next(
        (
            path
            for directory in ("active", "completed")
            if (path := root / "docs/exec-plans" / directory / f"{Path(args.sprint).name}.md").is_file()
        ),
        None,
    )
    if sprint is None:
        parser.error(f"Sprint 计划不存在: {args.sprint}")
    rules = load_yaml(HarnessPaths.detect(project=root).rules / "task-rules.yml")
    task = (rules.get("tasks") or {}).get("integration") if isinstance(rules, dict) else None
    if not isinstance(task, dict):
        parser.error("task-rules.yml 缺少 integration 任务")
    errors = validate_integration_receipt(root, sprint, args.task_id, task, load_harness_config())
    for error in errors:
        print(f"❌ {error}")
    if errors:
        return 1
    print(f"✅ integration receipt 通过: {args.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate the immutable receipt for the current promote-test task attempt."""

from __future__ import annotations

import argparse
from pathlib import Path

from mai_harness.runtime.application.integration_contract import validate_promote_test_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    _, errors = validate_promote_test_receipt(Path.cwd(), Path(args.sprint).name, args.task_id)
    for error in errors:
        print(f"❌ {error}")
    if errors:
        return 1
    print(f"✅ promote-test 回执通过: {args.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

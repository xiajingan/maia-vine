#!/usr/bin/env python3
"""Deprecated secrets export compatibility entry."""

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(
        description="已废弃：请改用 uv run --project .harness/runtime harness promote-prep <env> 生成 .harness/secrets/<env>.sh"
    )
    parser.parse_args()
    parser.error(
        "运行时 secrets 已迁移到 .harness/secrets/<env>.sh；请改用 uv run --project .harness/runtime harness promote-prep <env>"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

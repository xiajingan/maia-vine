#!/usr/bin/env python3
"""Validate branch-to-deployment-environment mapping."""

from __future__ import annotations

import argparse
import os
import re

from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import current_branch, err, ok


def detected_branch() -> str:
    return (
        os.environ.get("GITHUB_HEAD_REF")
        or os.environ.get("GITHUB_REF_NAME")
        or os.environ.get("CI_COMMIT_REF_NAME")
        or current_branch()
    )


def allowed(environment: str, branch: str, rules: dict[str, list[str]] | None = None) -> bool:
    rules = rules or load_harness_config()["delivery"]["branches"]
    if environment not in rules:
        raise ValueError(f"未知环境：{environment}")
    return any(re.search(pattern, branch) for pattern in rules[environment])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    rules = load_harness_config()["delivery"]["branches"]
    parser.add_argument("--env", choices=rules, required=True)
    parser.add_argument("--branch")
    args = parser.parse_args()
    branch = args.branch or detected_branch()
    if not allowed(args.env, branch, rules):
        err(f"分支 {branch} 不允许部署到 {args.env}（CICD.md）")
        err(f"允许模式：{', '.join(rules[args.env])}")
        return 1
    ok(f"branch={branch} → env={args.env}：映射通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

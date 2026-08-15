#!/usr/bin/env python3
"""Backfill lifecycle fields in legacy YAML test cases without rewriting YAML."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import info, ok, warn

FIELDS = ("introduced_in", "last_modified_in", "last_verified_in")


def backfill(text: str) -> tuple[str, str | None]:
    if all(re.search(rf"^{field}\s*:", text, re.M) for field in FIELDS):
        return text, None
    match = re.search(r"^(sprint\s*:\s*)([^\n#]+?)(\s*(?:#.*)?)$", text, re.M)
    if not match:
        return text, None
    sprint = match.group(2).strip()
    additions = [
        f"{field}: {sprint}  # backfilled from legacy sprint"
        for field in FIELDS
        if not re.search(rf"^{field}\s*:", text, re.M)
    ]
    return text[: match.end()] + "\n" + "\n".join(additions) + text[match.end() :], sprint


def run(directory: Path, *, write: bool = False) -> tuple[int, int, int]:
    files = sorted((*directory.rglob("*.yml"), *directory.rglob("*.yaml"))) if directory.exists() else []
    modified = skipped = 0
    for file in files:
        text = file.read_text(encoding="utf-8")
        updated, sprint = backfill(text)
        if updated == text:
            skipped += 1
            if not all(re.search(rf"^{field}\s*:", text, re.M) for field in FIELDS):
                warn(f"{file}: 无 sprint: 行且新字段不完整，跳过")
            continue
        if write:
            file.write_text(updated, encoding="utf-8")
        modified += 1
        info(f"{'WRITE' if write else 'WOULD'}: {file} ← introduced/modified/verified: {sprint}")
    return len(files), modified, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--test-cases-dir", default=str(PATHS.test_cases))
    args = parser.parse_args()
    total, modified, skipped = run(Path(args.test_cases_dir), write=args.write)
    suffix = "（已写入）" if args.write else "（dry-run；加 --write 落盘）"
    ok(f"扫描 {total}，将修改 {modified}，跳过 {skipped}{suffix}")


if __name__ == "__main__":
    main()

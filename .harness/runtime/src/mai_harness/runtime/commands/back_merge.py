#!/usr/bin/env python3
"""Create auditable main-to-downstream back-merge pull requests."""

import argparse
import re
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute, harness_command


def required(argv: list[str], root: Path) -> str:
    result = execute(CommandSpec.argv_command(argv, cwd=root))
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--to", required=True)
    parser.add_argument("--reason", default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        required(["git", "fetch", "origin", "--no-tags"], root)
        sha = required(["git", "rev-parse", "--short=7", f"origin/{args.source}"], root)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", args.reason)[:24]
        for target in args.to.split(","):
            branch = f"back-merge/{target}-{sha}-{slug}"
            print(f"back-merge: {args.source} → {target}")
            if args.dry_run:
                continue
            exists = execute(
                CommandSpec.argv_command(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd=root)
            ).ok
            if exists:
                continue
            required(["git", "switch", "-c", branch, f"origin/{target}"], root)
            merge = execute(
                CommandSpec.argv_command(
                    [
                        "git",
                        "merge",
                        "--no-ff",
                        f"origin/{args.source}",
                        "-m",
                        f"back-merge({args.reason}): {args.source} → {target}",
                    ],
                    cwd=root,
                )
            )
            if not merge.ok:
                print(f"❌ 合并冲突，已停在 {branch}")
                return 2
            required(["git", "push", "-u", "origin", branch], root)
            required(
                [
                    *harness_command("pr-adapter", "create"),
                    "--base",
                    target,
                    "--head",
                    branch,
                    "--title",
                    f"back-merge: {args.source} → {target}",
                    "--labels",
                    "back-merge",
                ],
                root,
            )
        return 0
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

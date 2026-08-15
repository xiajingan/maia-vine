"""Execute small deterministic actions that do not warrant dedicated CLIs."""

from __future__ import annotations

import argparse
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action_id")
    parser.add_argument("--release")
    args = parser.parse_args()
    root = Path.cwd()
    if args.action_id == "release.directory.ensure":
        if not args.release:
            parser.error("release.directory.ensure 需要 --release")
        (root / "deploy/release" / args.release).mkdir(parents=True, exist_ok=True)
        return 0
    if args.action_id == "release.notes.exists":
        if not args.release:
            parser.error("release.notes.exists 需要 --release")
        return 0 if (root / "deploy/release" / args.release / "release-notes.md").is_file() else 1
    commands = {
        "vcs.fetch.main": ["git", "fetch", "origin", "main", "--tags"],
        "vcs.fetch.release-branches": ["git", "fetch", "origin", "main", "test", "develop", "--no-tags"],
    }
    if args.action_id not in commands:
        parser.error(f"未知内建 action: {args.action_id}")
    return execute(CommandSpec.argv_command(commands[args.action_id], cwd=root)).returncode


if __name__ == "__main__":
    raise SystemExit(main())

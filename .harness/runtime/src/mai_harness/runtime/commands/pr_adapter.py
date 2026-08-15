#!/usr/bin/env python3
"""Unified GitHub/GitLab/local pull-request adapter."""

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute


def platform(root: Path | None = None) -> str:
    root = root or Path.cwd()
    if os.environ.get("HARNESS_PLATFORM"):
        return os.environ["HARNESS_PLATFORM"]
    result = execute(CommandSpec.argv_command(["git", "config", "--get", "remote.origin.url"], cwd=root))
    url = result.stdout
    return "github" if re.search(r"github\.com[:/]", url) else "gitlab" if "gitlab." in url else "local"


def command(argv: list[str], root: Path) -> str:
    result = execute(CommandSpec.argv_command(argv, cwd=root))
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("platform")
    create = sub.add_parser("create")
    create.add_argument("--base", required=True)
    create.add_argument("--head", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body-file")
    create.add_argument("--labels")
    for action in ("status", "merge", "comment"):
        item = sub.add_parser(action)
        item.add_argument("--number", required=True)
        if action == "merge":
            item.add_argument("--method", choices=("squash", "merge", "rebase"), default="squash")
        if action == "comment":
            item.add_argument("--body-file", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    provider = platform(root)
    if args.action == "platform":
        print(provider)
        return 0
    if provider == "local":
        print("PR-LOCAL-0" if args.action == "create" else "open" if args.action == "status" else "ok")
        return 0
    try:
        if provider == "github":
            if not shutil.which("gh"):
                raise RuntimeError("gh CLI 未安装")
            if args.action == "create":
                argv = (
                    ["gh", "pr", "create", "--base", args.base, "--head", args.head, "--title", args.title]
                    + (["--body-file", args.body_file] if args.body_file else ["--body", ""])
                    + (["--label", args.labels] if args.labels else [])
                )
                print(command(argv, root))
            elif args.action == "status":
                data = json.loads(command(["gh", "pr", "view", args.number, "--json", "state,mergeable"], root))
                print(
                    "merged"
                    if data["state"] == "MERGED"
                    else "closed"
                    if data["state"] == "CLOSED"
                    else "conflicts"
                    if data.get("mergeable") == "CONFLICTING"
                    else "open"
                )
            elif args.action == "merge":
                command(["gh", "pr", "merge", args.number, f"--{args.method}", "--delete-branch"], root)
            else:
                command(["gh", "pr", "comment", args.number, "--body-file", args.body_file], root)
        else:
            if args.action == "create" and shutil.which("glab"):
                argv = [
                    "glab",
                    "mr",
                    "create",
                    "--target-branch",
                    args.base,
                    "--source-branch",
                    args.head,
                    "--title",
                    args.title,
                    "--description",
                    Path(args.body_file).read_text() if args.body_file else "",
                ] + (["--label", args.labels] if args.labels else [])
                print(command(argv, root))
            elif args.action == "create":
                argv = [
                    "git",
                    "push",
                    "-u",
                    "-o",
                    "merge_request.create",
                    "-o",
                    f"merge_request.target={args.base}",
                    "-o",
                    f"merge_request.title={args.title}",
                    "-o",
                    "merge_request.remove_source_branch",
                    "origin",
                    f"HEAD:{args.head}",
                ]
                print(command(argv, root))
            elif not shutil.which("glab"):
                raise RuntimeError("glab CLI 未安装")
            elif args.action == "status":
                data = json.loads(command(["glab", "mr", "view", args.number, "--output", "json"], root))
                print(
                    "merged"
                    if data["state"] == "merged"
                    else "closed"
                    if data["state"] == "closed"
                    else "conflicts"
                    if data.get("merge_status") == "cannot_be_merged"
                    else "open"
                )
            elif args.action == "merge":
                print(f"GitLab MR !{args.number} 必须人工合并")
            else:
                command(["glab", "mr", "note", args.number, "--message", Path(args.body_file).read_text()], root)
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

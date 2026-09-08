#!/usr/bin/env python3
"""Unified GitHub/GitLab/local pull-request adapter."""

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.harness_config import valid_git_branch

GITLAB_MR_URL = re.compile(r"https://[^\s<>\"']+?/-/merge_requests/[0-9]+(?=$|[\s)\],.;])")


def gitlab_description(body_file: str | None) -> str:
    """Encode an optional UTF-8 body for GitLab push options."""

    if not body_file:
        return ""
    body = Path(body_file).read_text(encoding="utf-8")
    if "\x00" in body:
        raise ValueError("MR description 不能包含 NUL")
    return body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")


def gitlab_source_branch(head: str, base: str) -> str:
    """Derive a stable adapter-owned source branch for one target MR."""

    if not valid_git_branch(head) or not valid_git_branch(base):
        raise ValueError("GitLab source/target branch 非法")
    digest = hashlib.sha256(f"{head}\0{base}".encode()).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", head).strip("-.")[:80] or "head"
    return f"harness/mr/{slug}/{digest}"


def gitlab_fallback_create_argv(args: argparse.Namespace) -> list[str]:
    """Build the shell-free GitLab push-options fallback command."""

    source = gitlab_source_branch(args.head, args.base)
    description = gitlab_description(args.body_file)
    argv = [
        "git",
        "push",
        "-o",
        "merge_request.create",
        "-o",
        f"merge_request.target={args.base}",
        "-o",
        f"merge_request.title={args.title}",
        "-o",
        f"merge_request.description={description}",
    ]
    if args.labels:
        argv.extend(["-o", f"merge_request.label={args.labels}"])
    argv.extend(["origin", f"HEAD:{source}"])
    return argv


def gitlab_fallback_mr_url(stdout: str, stderr: str) -> str:
    """Return the unique canonical MR URL reported by GitLab's push sideband."""

    urls = set(GITLAB_MR_URL.findall(f"{stdout}\n{stderr}"))
    if len(urls) != 1:
        raise RuntimeError(f"GitLab push 未返回唯一 MR URL（找到 {len(urls)} 个）")
    return urls.pop()


def gitlab_fallback_create(args: argparse.Namespace, root: Path) -> dict[str, str]:
    """Create a GitLab MR through push options and retain its remote identity."""

    argv = gitlab_fallback_create_argv(args)
    result = execute(CommandSpec.argv_command(argv, cwd=root))
    if not result.ok:
        raise RuntimeError(result.stderr or result.stdout)
    url = gitlab_fallback_mr_url(result.stdout, result.stderr)
    return {
        "url": url,
        "source": argv[-1].removeprefix("HEAD:"),
        "target": args.base,
        "receipt": url,
    }


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
                print(json.dumps(gitlab_fallback_create(args, root), ensure_ascii=False))
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
    except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

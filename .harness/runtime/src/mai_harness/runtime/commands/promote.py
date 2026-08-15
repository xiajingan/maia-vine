#!/usr/bin/env python3
"""Promote immutable development artifacts into the test environment."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.delivery_mode import resolve_delivery_mode
from mai_harness.runtime.infrastructure.core.command import harness_command
from mai_harness.runtime.infrastructure.deploy_config import load_build_targets_compat, load_environments_compat
from mai_harness.runtime.infrastructure.utils import fatal, info, ok, run, run_capture, try_run, warn


def source_ref(requested: str, dry_run: bool) -> str:
    for candidate in dict.fromkeys((requested, "origin/develop", "develop", "origin/main", "main", "HEAD")):
        if try_run(["git", "rev-parse", "--verify", "--quiet", candidate]).ok:
            if candidate != requested:
                warn(f"source ref {requested} 不存在，回退到 {candidate}")
            return candidate
    fatal(f"{'dry-run ' if dry_run else ''}无法解析 source ref: {requested}")


def append_log(identifier: str, sha: str, branch: str) -> None:
    path = Path(".harness/state/promotion-log.yml")
    previous = path.read_text(encoding="utf-8") if path.exists() else "log:\n"
    if not previous.startswith("log:\n"):
        previous = "log:\n" + previous
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        previous
        + f"- kind: promote\n  id: {identifier}\n  sha: {sha}\n  branch: {branch}\n  ts: {datetime.now(UTC).isoformat()}\n",
        encoding="utf-8",
    )


def promote(args: argparse.Namespace) -> None:
    if not load_environments_compat().get("environments", {}).get("test", {}).get("enabled"):
        info("deploy.yml: environments.test.enabled=false → 跳过 promote")
        return
    identifier = f"train-{datetime.now(UTC).date()}" if args.train else (args.sprints or "manual").replace(",", "-")
    try_run(["git", "fetch", "origin", "--no-tags"])
    source = source_ref(args.source_ref, args.dry_run)
    sha_result = try_run(["git", "rev-parse", "--short", source])
    if not sha_result.ok:
        fatal(f"无法解析 source ref: {source}")
    sha = sha_result.stdout.strip()
    branch = f"promote/{identifier}"
    info(f"promote: id={identifier} branch={branch} source={source} sha={sha} dry-run={args.dry_run}")
    if args.dry_run:
        ok("promote dry-run 完成（未获取锁、未创建分支、未写日志、未创建 PR）")
        return
    owner = f"promote-{identifier}"
    run(harness_command("lock", "acquire", "test", "--owner", owner, "--ttl", "7200"))
    try:
        run(["git", "switch", "-c", branch, source])
        build = load_build_targets_compat()
        if resolve_delivery_mode() == "artifact":
            targets = list(build.get("targets", {}))
            if not targets:
                fatal("artifact 模式 promote 需要 build.targets")
            artifact_dir, repository = build.get("artifact_dir", ".harness/images"), build.get("image_repo", "qwchat")
            for target in targets:
                run(
                    harness_command(
                        "image-promote",
                        "--from",
                        f"dev-{sha}",
                        "--to",
                        f"test-{sha}",
                        "--from-tar",
                        f"{artifact_dir}/{repository}-{target}-dev-{sha}.tar",
                        "--to-tar",
                        f"{artifact_dir}/{repository}-{target}-test-{sha}.tar",
                    )
                )
        else:
            run(harness_command("image-promote", "--from", f"dev-{sha}", "--to", f"test-{sha}"))
        run(["git", "push", "-u", "origin", branch])
        url = run_capture(
            harness_command(
                "pr-adapter",
                "create",
                "--base",
                "test",
                "--head",
                branch,
                "--title",
                f"promote({identifier}): {sha}",
                "--labels",
                "promote",
            )
        )
        info(f"PR/MR: {url}")
        append_log(identifier, sha, branch)
        ok("promote 流水线已启动")
    except BaseException:
        released = try_run(harness_command("lock", "release", "test", "--owner", owner))
        if not released.ok:
            warn("promote 失败，且未能自动释放 test 锁")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test")
    test.add_argument("--sprints")
    test.add_argument("--train", action="store_true")
    test.add_argument("--source-ref", default="origin/develop")
    test.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "status":
        path = Path(".harness/state/promotion-log.yml")
        print(path.read_text(encoding="utf-8") if path.exists() else "无 promote 历史", end="")
    else:
        promote(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

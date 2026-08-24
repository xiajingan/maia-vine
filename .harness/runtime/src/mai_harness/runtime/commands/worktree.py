#!/usr/bin/env python3
"""Low-level, safe Git worktree operations used by Sprint lifecycle commands."""

import argparse
import os
import re
from pathlib import Path

from mai_harness.runtime.application.worktree_service import allocated_ports, create_linked_worktree
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.harness_config import load_harness_config

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def ports(task_id: str) -> tuple[int, int]:
    config = load_harness_config()["worktree"]
    return allocated_ports(config, task_id)


def run(argv: list[str], cwd: Path | None = None, required: bool = True) -> bool:
    outcome = execute(CommandSpec.argv_command(argv, cwd=cwd))
    if required and not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout)
    return outcome.ok


def branch_name(identifier: str) -> str:
    return f"sprint/{identifier.removeprefix('sprint-')}" if identifier.startswith("sprint-") else f"task/{identifier}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("task_id")
    create.add_argument("base_ref", help="必须显式指定经过验证的远端基线 ref/SHA；禁止隐式 HEAD")
    destroy = sub.add_parser("destroy")
    destroy.add_argument("task_id")
    destroy.add_argument("--merged-into", required=True, help="删除前必须证明任务分支已抵达该 ref")
    recover = sub.add_parser("recover-destroy", help="人工恢复专用：强制删除并明确报告目标")
    recover.add_argument("task_id")
    sub.add_parser("list")
    port_parser = sub.add_parser("ports")
    port_parser.add_argument("task_id")
    args = parser.parse_args()
    if hasattr(args, "task_id") and not IDENTIFIER.fullmatch(args.task_id):
        parser.error("worktree ID 只能包含字母、数字、点、下划线和连字符")
    worktree_config = load_harness_config()["worktree"]
    worktree_root = Path(worktree_config["root"])
    if args.command == "ports":
        api, web = ports(args.task_id)
        print(f"API_PORT={api}\nWEB_PORT={web}")
        return 0
    if args.command == "list":
        for path in sorted(worktree_root.iterdir()) if worktree_root.exists() else []:
            if path.is_dir():
                api, web = ports(path.name)
                print(f"{path.name:20} {api:<8} {web:<8} {path}/")
        return 0
    path = worktree_root / args.task_id
    branch = branch_name(args.task_id)
    try:
        if args.command == "create":
            create_linked_worktree(Path.cwd(), path, branch, args.base_ref, args.task_id, worktree_config)
            print(path)
            return 0
        if args.command == "recover-destroy":
            print(f"⚠️ 强制恢复删除: worktree={path} branch={branch}")
            run(["git", "worktree", "remove", str(path), "--force"])
            run(["git", "branch", "-D", branch])
            return 0
        if not path.is_dir():
            raise FileNotFoundError(f"Worktree 不存在: {path}")
        outcome = execute(CommandSpec.argv_command(("git", "status", "--porcelain"), cwd=path))
        if not outcome.ok:
            raise RuntimeError(outcome.stderr or "无法读取 worktree 状态")
        if outcome.stdout.strip():
            raise RuntimeError("Worktree 存在 staged/unstaged/untracked 修改，拒绝删除")
        if not run(["git", "merge-base", "--is-ancestor", branch, args.merged_into], required=False):
            raise RuntimeError(f"分支 {branch} 尚未抵达 {args.merged_into}，拒绝删除")
        pids = path / ".harness/pids"
        if pids.exists():
            for value in pids.read_text().split():
                try:
                    os.kill(int(value), 15)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        run(["git", "worktree", "remove", str(path)])
        run(["git", "branch", "-d", branch])
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

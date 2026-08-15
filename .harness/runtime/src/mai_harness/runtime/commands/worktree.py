#!/usr/bin/env python3
"""Create and clean deterministic per-task Git worktrees."""

import argparse
import os
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def posix_cksum(value: str) -> int:
    data = value.encode()
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    length = len(data)
    while length:
        crc ^= (length & 0xFF) << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        length >>= 8
    return (~crc) & 0xFFFFFFFF


def ports(task_id: str) -> tuple[int, int]:
    config = load_harness_config()["worktree"]
    offset = posix_cksum(task_id) % int(config["port_range"]) + 1
    return int(config["ports"]["api_base"]) + offset, int(config["ports"]["web_base"]) + offset


def run(argv: list[str], cwd: Path | None = None, required: bool = True) -> bool:
    outcome = execute(CommandSpec.argv_command(argv, cwd=cwd))
    if required and not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout)
    return outcome.ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("task_id")
    create.add_argument("base_ref", nargs="?", default="HEAD")
    destroy = sub.add_parser("destroy")
    destroy.add_argument("task_id")
    sub.add_parser("list")
    port_parser = sub.add_parser("ports")
    port_parser.add_argument("task_id")
    args = parser.parse_args()
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
    try:
        if args.command == "create":
            if path.exists():
                raise FileExistsError(f"Worktree 已存在: {path}")
            run(["git", "worktree", "add", str(path), "-b", f"task/{args.task_id}", args.base_ref])
            for adapter in worktree_config["dependency_adapters"]:
                marker, command = adapter["marker"], adapter["command"]
                if (path / marker).exists():
                    run(command, path, required=False)
                    break
            api, web = ports(args.task_id)
            state = path / ".harness"
            state.mkdir(parents=True)
            (state / "ports").write_text(f"API_PORT={api}\nWEB_PORT={web}\n", encoding="utf-8")
            print(path)
            return 0
        pids = path / ".harness/pids"
        if pids.exists():
            for value in pids.read_text().split():
                try:
                    os.kill(int(value), 15)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        run(["git", "worktree", "remove", str(path), "--force"], required=False)
        run(["git", "branch", "-D", f"task/{args.task_id}"], required=False)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

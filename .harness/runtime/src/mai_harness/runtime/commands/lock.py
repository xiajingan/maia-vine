#!/usr/bin/env python3
"""File-backed environment deployment lock with TTL."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.core.state_store import StateStore

DEFAULT_TTL = 7200


def lock_path(environment: str, state_dir: Path = Path(".harness/state")) -> Path:
    return state_dir / f"{environment}.lock"


def read_lock(environment: str, state_dir: Path = Path(".harness/state")) -> dict | None:
    return StateStore(state_dir).read_json(f"{environment}.lock")


def expired(lock: dict, now: datetime | None = None) -> bool:
    acquired = lock.get("acquired_at", 0)
    timestamp = (
        datetime.fromisoformat(acquired.replace("Z", "+00:00")).timestamp()
        if isinstance(acquired, str)
        else float(acquired)
    )
    return (now or datetime.now(UTC)).timestamp() - timestamp > int(lock["ttl_seconds"])


def acquire(environment: str, owner: str, ttl: int, state_dir: Path = Path(".harness/state")) -> tuple[bool, str]:
    success = StateStore(state_dir).acquire(f"{environment}.lock", owner, ttl)
    current = read_lock(environment, state_dir) or {}
    message = (
        f"已获取锁：{environment} → {owner}（TTL {ttl}s）"
        if success
        else f"环境 {environment} 已被 {current.get('owner', 'unknown')} 持有"
    )
    return success, message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("acquire", "release"):
        item = sub.add_parser(command)
        item.add_argument("env")
        item.add_argument("--owner", required=True)
        if command == "acquire":
            item.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    for command in ("check", "owner", "force-release"):
        item = sub.add_parser(command)
        item.add_argument("env")
    args = parser.parse_args()
    current = read_lock(args.env)
    if args.command == "acquire":
        success, message = acquire(args.env, args.owner, args.ttl)
        print(message)
        return 0 if success else 1
    if args.command == "release":
        if not current:
            print(f"无锁可释放：{args.env}")
            return 0
        if current["owner"] != args.owner and not expired(current):
            print(f"只能由持有者释放，当前持有者：{current['owner']}")
            return 1
        StateStore(Path(".harness/state")).release(f"{args.env}.lock", args.owner)
        print(f"已释放锁：{args.env}")
        return 0
    if args.command == "force-release":
        StateStore(Path(".harness/state")).release(f"{args.env}.lock", "", force=True)
        print(f"强制释放：{args.env}")
        return 0
    active = current and not expired(current)
    if args.command == "owner":
        if active:
            print(current["owner"], end="")
        return 0
    if not active:
        print(f"expired {current['owner']}" if current else "free")
        return 0
    print(f"held {current['owner']} acquired_at={current['acquired_at']} ttl={current['ttl_seconds']}s")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run one project command declared in config/harness.yml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.harness_config import (
    load_harness_config,
    resolve_command,
    resolve_command_group,
)


def run(command: list[str]) -> int:
    outcome = execute(CommandSpec.argv_command(command, cwd=Path.cwd()))
    if outcome.stdout:
        print(outcome.stdout, end="")
    if outcome.stderr:
        print(outcome.stderr, file=sys.stderr, end="")
    return outcome.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--group")
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    if bool(args.name) == bool(args.group):
        parser.error("必须且只能指定命令名称或 --group")
    config = load_harness_config()
    if args.group:
        try:
            commands = resolve_command_group(config, args.group, require_conditions=True)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        if not commands:
            print(f"⏭️ command_groups.{args.group} 未配置或为空")
            return 2 if args.required else 0
        for command in commands:
            returncode = run(command)
            if returncode:
                return returncode
        return 0
    command = config.get("commands", {}).get(args.name, [])
    from mai_harness.runtime.infrastructure.harness_config import command_enabled

    if not command_enabled(config, args.name, Path.cwd()):
        print(f"❌ commands.{args.name} 条件未满足", file=sys.stderr)
        return 2
    if not command:
        print(f"⏭️ commands.{args.name} 未配置")
        return 2 if args.required else 0
    return run(resolve_command(command))


if __name__ == "__main__":
    raise SystemExit(main())

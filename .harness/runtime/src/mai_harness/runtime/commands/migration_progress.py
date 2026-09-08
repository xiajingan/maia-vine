#!/usr/bin/env python3
"""Advance long-running migrations without rolling candidate data back."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mai_harness.runtime.application.migration_progress import (
    begin,
    commit_checkpoint,
    complete,
    load_progress,
    record_failure,
    resume,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml


def configured_failure_phases(root: Path) -> list[str]:
    rules_path = HarnessPaths.detect(project=root).rules / "task-rules.yml"
    policy = (load_yaml(rules_path) if rules_path.is_file() else {}).get("migration_execution_policy", {})
    phases = policy.get("failure_phases") if isinstance(policy, dict) else None
    if not isinstance(phases, list) or not phases or not all(isinstance(item, str) for item in phases):
        raise ValueError(f"migration_execution_policy.failure_phases 无效: {rules_path}")
    return phases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    begin_parser = sub.add_parser("begin")
    begin_parser.add_argument("migration_id")
    begin_parser.add_argument("--candidate-ref", required=True)
    begin_parser.add_argument("--workflow-kind", default="migration")
    begin_parser.add_argument("--required-checkpoint", action="append", default=[])
    checkpoint_parser = sub.add_parser("checkpoint")
    checkpoint_parser.add_argument("migration_id")
    checkpoint_parser.add_argument("checkpoint_id")
    checkpoint_parser.add_argument("--summary", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("migration_id")
    run_parser.add_argument("--phase", required=True)
    for command in ("fail", "resume", "complete", "status"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("migration_id")
        if command == "fail":
            command_parser.add_argument("--phase", required=True)
            command_parser.add_argument("--reason", required=True)
    raw_args = sys.argv[1:]
    command_argv: list[str] = []
    if "run" in raw_args and "--" in raw_args:
        separator = raw_args.index("--")
        command_argv = raw_args[separator + 1 :]
        raw_args = raw_args[:separator]
    args = parser.parse_args(raw_args)
    try:
        phases = configured_failure_phases(args.root) if args.command in {"run", "fail"} else []
        if args.command == "begin":
            state = begin(
                args.root,
                args.migration_id,
                args.candidate_ref,
                workflow_kind=args.workflow_kind,
                required_checkpoints=args.required_checkpoint,
            )
        elif args.command == "checkpoint":
            state = commit_checkpoint(args.root, args.migration_id, args.checkpoint_id, args.summary)
        elif args.command == "fail":
            state = record_failure(args.root, args.migration_id, args.phase, args.reason, allowed_phases=phases)
        elif args.command == "run":
            if not command_argv:
                parser.error("run 必须在 -- 后提供 argv 命令")
            if args.phase not in phases:
                parser.error(f"phase 必须是 {phases}")
            state = load_progress(args.root, args.migration_id)
            if state.get("status") != "active":
                parser.error("run 只允许 active 迁移；失败后先修复并 resume")
            outcome = execute(CommandSpec.argv_command(command_argv, cwd=args.root.resolve()))
            if outcome.stdout:
                print(outcome.stdout, end="" if outcome.stdout.endswith("\n") else "\n")
            if outcome.stderr:
                print(outcome.stderr, file=sys.stderr, end="" if outcome.stderr.endswith("\n") else "\n")
            if not outcome.ok:
                state = record_failure(
                    args.root,
                    args.migration_id,
                    args.phase,
                    f"command exited {outcome.returncode}",
                    allowed_phases=phases,
                )
                print(json.dumps(state, ensure_ascii=False, indent=2))
                return outcome.returncode or 1
        elif args.command == "resume":
            state = resume(args.root, args.migration_id)
        elif args.command == "complete":
            state = complete(args.root, args.migration_id)
        else:
            state = load_progress(args.root, args.migration_id)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

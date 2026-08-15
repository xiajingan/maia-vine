#!/usr/bin/env python3
"""Execute a policy-bounded deterministic fixer for one Heartbeat finding."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mai_harness.runtime.application.autofix import run_safe_fix
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute, harness_command
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import try_run


def run_fixer(command: str, root: Path) -> None:
    """Run a configured fixer and surface failure to the safe-fix state machine."""
    outcome = execute(CommandSpec.shell(command, cwd=root))
    if not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout or f"fixer exited {outcome.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fingerprint")
    args = parser.parse_args()
    root = Path.cwd()
    config = load_harness_config()
    automation = config["automation"]
    store = StateStore(root / automation["state_dir"])
    finding = store.read_json(f"findings/{args.fingerprint}.json")
    if not finding:
        parser.error(f"finding not found: {args.fingerprint}")
    policy = {**automation.get("autonomy", {}), "max_attempts": automation["max_fix_attempts"]}
    fix_command = policy.get("fix_commands", {}).get(finding["job"])
    if not fix_command:
        parser.error(f"safe-fix command not configured for job: {finding['job']}")
    if try_run(["git", "status", "--porcelain"], cwd=root).stdout.strip():
        parser.error("safe-fix requires a clean worktree")

    def validate(_: list[str]) -> dict:
        commands = [
            harness_command("secrets-scan", "scan"),
            harness_command("validate-task-rules"),
            harness_command("run-project-command", "unit"),
            harness_command("heartbeat", "run", finding["job"], "--force"),
        ]
        outcomes = [execute(CommandSpec.argv_command(command, cwd=root)) for command in commands]
        return {"ok": all(item.ok for item in outcomes), "commands": [item.display for item in outcomes]}

    def create_pr(context: dict) -> str:
        branch = f"automation/{finding['job']}-{args.fingerprint[:10]}"
        for command in (
            ["git", "switch", "-c", branch],
            ["git", "add", "--all"],
            ["git", "commit", "-m", f"fix({finding['job']}): resolve {args.fingerprint[:10]}"],
            ["git", "push", "-u", "origin", branch],
        ):
            outcome = execute(CommandSpec.argv_command(command, cwd=root))
            if not outcome.ok:
                raise RuntimeError(outcome.stderr or outcome.stdout)
        outcome = execute(
            CommandSpec.argv_command(
                [
                    *harness_command("pr-adapter", "create"),
                    "--base",
                    os.environ.get("HARNESS_AUTOFIX_BASE", "develop"),
                    "--head",
                    branch,
                    "--title",
                    f"[automation] {finding['message']}",
                ],
                cwd=root,
            )
        )
        if not outcome.ok:
            raise RuntimeError(outcome.stderr or outcome.stdout)
        return outcome.stdout.strip()

    result = run_safe_fix(
        policy=policy,
        finding=finding,
        fix=lambda _: run_fixer(fix_command, root),
        changed_files=lambda: try_run(["git", "diff", "--name-only"], cwd=root).stdout.splitlines(),
        validate=validate,
        create_pr=create_pr,
    )
    store.write_json(f"findings/{args.fingerprint}.json", result["finding"])
    store.write_json(f"autofix/{args.fingerprint}-{result['finding'].get('attempts', 0)}.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "triage" else 0


if __name__ == "__main__":
    raise SystemExit(main())

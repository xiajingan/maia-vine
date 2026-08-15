#!/usr/bin/env python3
"""Idempotent Automation Heartbeat entry point."""

from __future__ import annotations

import argparse
import os
import re
from datetime import UTC, datetime
from typing import Any

from mai_harness.runtime.application.automation_state import (
    acquire_lock,
    classify_failure,
    gc_automation_state,
    list_findings,
    release_lock,
    run_key,
    stable_hash,
    state_paths,
    upsert_finding,
    write_json,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.harness_config import load_harness_config, resolve_command
from mai_harness.runtime.infrastructure.utils import run_capture


def load_jobs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = config.get("automation", {}).get("jobs", {})
    if not jobs:
        raise ValueError("automation.jobs 不能为空")
    return jobs


def git_sha() -> str:
    try:
        return run_capture(["git", "rev-parse", "HEAD"])
    except Exception:
        return "nogit"


def extract_findings(
    job: str, output: str, mode: str = "failure", run_id: str | None = None, exit_code: int = 0
) -> list[dict[str, Any]]:
    findings = []
    if mode == "garden":
        for severity, path, message in re.findall(
            r"^- \*\*(high|medium|low)\*\* `([^`]+)` — (.+)$", output, re.MULTILINE
        ):
            findings.append(
                {
                    "job": job,
                    "rule": f"{job}:scan",
                    "severity": severity,
                    "path": path,
                    "message": message,
                    "category": "code",
                    "run_id": run_id,
                }
            )
    if exit_code and not findings:
        findings.append(
            {
                "job": job,
                "rule": f"{job}:execution",
                "severity": "high",
                "message": output.strip().splitlines()[-1] if output.strip() else f"job exited {exit_code}",
                "category": classify_failure(exit_code, output),
                "run_id": run_id,
            }
        )
    return findings


def execute_job(
    job: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    source_sha: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or load_harness_config()
    automation = config["automation"]
    jobs = load_jobs(config)
    if not automation["enabled"]:
        raise RuntimeError("automation.enabled=false")
    if job not in jobs:
        raise ValueError(f"unknown heartbeat job: {job}")
    source_sha = source_sha or git_sha()
    config_hash = stable_hash(automation)
    identifier = run_key(job=job, source_sha=source_sha, config_hash=config_hash)
    paths = state_paths(automation["state_dir"], job, identifier)
    if not force and paths["run"].exists():
        import json

        previous = json.loads(paths["run"].read_text(encoding="utf-8"))
        if previous.get("status") == "passed":
            return {**previous, "skipped": True}
    owner = f"{os.getpid()}-{identifier[:12]}"
    if not acquire_lock(paths["lock"], owner, automation["lock_ttl_seconds"]):
        return {"id": identifier, "job": job, "status": "locked", "skipped": True}
    started = datetime.now(UTC).isoformat()
    try:
        if dry_run:
            result: dict[str, Any] = {"status": "planned", "exit_code": 0}
        elif jobs[job].get("internal") == "gc":
            result = {
                "status": "passed",
                "exit_code": 0,
                "removed": gc_automation_state(
                    automation["state_dir"], lock_ttl_seconds=automation["lock_ttl_seconds"]
                ),
            }
        else:
            command = resolve_command(jobs[job]["command"])
            process = execute(CommandSpec.argv_command(command))
            result = {
                "status": "passed" if process.ok else "failed",
                "exit_code": process.returncode,
                "output": (process.stdout + process.stderr)[-8000:],
            }
    finally:
        release_lock(paths["lock"], owner)
    record = {
        "id": identifier,
        "job": job,
        "source_sha": source_sha,
        "config_hash": config_hash,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        **result,
    }
    extracted = extract_findings(
        job,
        record.get("output", ""),
        jobs[job].get("finding_parser", "failure"),
        identifier,
        record["exit_code"],
    )
    record["findings"] = [upsert_finding(automation["state_dir"], finding) for finding in extracted]
    write_json(paths["run"], record)
    write_json(paths["latest"], record)
    return record


def main() -> int:
    config = load_harness_config()
    jobs = load_jobs(config)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("job", choices=jobs)
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("--dry-run", action="store_true")
    sub.add_parser("findings")
    args = parser.parse_args()
    if args.command == "findings":
        import json

        print(json.dumps(list_findings(load_harness_config()["automation"]["state_dir"]), ensure_ascii=False, indent=2))
        return 0
    result = execute_job(args.job, force=args.force, dry_run=args.dry_run, config=config)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result.get("exit_code", 1) if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

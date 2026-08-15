#!/usr/bin/env python3
"""Platform-neutral local delivery pipeline state machine."""

from __future__ import annotations

import argparse
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.pipeline_plan import create_pipeline_plan, stage_input_hash, validate_artifact_evidence
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute, harness_command
from mai_harness.runtime.infrastructure.core.context import HarnessContext
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import try_run


def git_sha(root: Path) -> str:
    result = try_run(["git", "rev-parse", "HEAD"], cwd=root)
    return result.stdout.strip() if result.ok else "nogit"


def plan_pipeline(options: dict[str, Any], config: dict[str, Any], store: StateStore, root: Path) -> dict[str, Any]:
    plan = create_pipeline_plan(
        config,
        env=options["env"],
        target=options.get("target", "all"),
        profile=options.get("profile", "release"),
        source_sha=options.get("source_sha") or git_sha(root),
    )
    source = store.read_json(f"runs/{options['from_run_id']}.json") if options.get("from_run_id") else None
    if options.get("from_run_id") and source is None:
        raise ValueError(f"promotion source run not found: {options['from_run_id']}")
    if source and (source.get("status") != "passed" or not source.get("artifact_evidence")):
        raise ValueError("promotion source must be passed and contain immutable artifact evidence")
    if options["env"] == "prod" and not source:
        raise ValueError("prod requires --from-run-id; production promotion must not rebuild")
    run_id = f"{options['env']}-{plan['plan_hash'][:12]}" + (f"-{options['from_run_id'][-6:]}" if source else "")
    stable = store.read_json(f"stable/{options['env']}.json")
    return {
        "run_id": run_id,
        "status": "planned",
        "created_at": datetime.now(UTC).isoformat(),
        **plan,
        "promoted_from_run_id": source.get("run_id", "") if source else "",
        "previous_stable": stable,
        "artifact_evidence": source.get("artifact_evidence", []) if source else [],
        "stages": [],
    }


def run_pipeline(manifest: dict[str, Any], root: Path, store: StateStore, dry_run: bool = False) -> dict[str, Any]:
    stages = []
    previous = {item["name"]: item for item in manifest.get("stages", []) if item.get("status") == "passed"}

    def stage(name: str, argv: list[str], environment: dict[str, str] | None = None) -> bool:
        spec = {"name": name, "command": argv, "env": environment or {}}
        input_hash = stage_input_hash(spec, manifest)
        if previous.get(name, {}).get("input_hash") == input_hash:
            stages.append({**previous[name], "resumed_skip": True})
            return True
        record = {"name": name, "command": argv, "input_hash": input_hash, "started_at": datetime.now(UTC).isoformat()}
        outcome = None if dry_run else execute(CommandSpec.argv_command(argv, cwd=root, env=environment or {}))
        record.update(
            {
                "status": "planned" if dry_run else "passed" if outcome and outcome.ok else "failed",
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        if outcome and not outcome.ok:
            record.update({"exit_code": outcome.returncode, "error": (outcome.stderr or outcome.stdout)[-2000:]})
        stages.append(record)
        return dry_run or bool(outcome and outcome.ok)

    commands = [
        ("validate-inputs", harness_command("env-check", "validate")),
        ("project-tests", harness_command("run-project-command", "unit")),
    ]
    try:
        for name, command in commands:
            if not stage(name, command):
                raise RuntimeError(f"stage failed: {name}")
        if not manifest.get("promoted_from_run_id"):
            for target in manifest["targets"]:
                name = target["name"]
                if not stage(
                    f"package-push:{name}",
                    harness_command("build-image", "--sprint", manifest["run_id"], "--target", name),
                    {
                        "HARNESS_BUILD_PLATFORM": ",".join(target["platforms"]),
                        "HARNESS_DELIVERY_MODE": target["delivery"],
                    },
                ):
                    raise RuntimeError(f"build failed: {name}")
                if not dry_run:
                    state = json.loads((root / f".harness/state/build-image-{manifest['run_id']}.json").read_text())
                    images = [item for item in state.get("images", []) if item.get("target") == name]
                    manifest["artifact_evidence"] = [
                        item for item in manifest.get("artifact_evidence", []) if item.get("target") != name
                    ] + validate_artifact_evidence(images, [target])
        if not dry_run:
            validate_artifact_evidence(manifest["artifact_evidence"], manifest["targets"])
        if not stage("deploy-preflight", harness_command("promote-prep", manifest["env"])):
            raise RuntimeError("deploy preflight failed")
        evidence = manifest.get("artifact_evidence") or []
        tag = evidence[0].get("ref", "").rsplit(":", 1)[-1] if evidence else "<verified-tag>"
        batch_size = 1 if manifest["strategy"] == "serial" else int(manifest["strategy_config"].get("batch_size", 1))
        failure_threshold = int(manifest["strategy_config"].get("failure_threshold", 0))
        deployed: list[dict[str, Any]] = []
        failures: list[str] = []
        nodes = manifest["nodes"]
        for offset in range(0, len(nodes), batch_size):
            batch = nodes[offset : offset + batch_size]
            for node in batch:
                env = {"HARNESS_DEPLOY_TARGET": node["deploy_target"], "HARNESS_DEPLOY_NODE_ID": node["id"]}
                passed = stage(
                    f"deploy:{node['id']}",
                    harness_command("deploy", "--env", manifest["env"], "--tag", tag or "<verified-tag>"),
                    env,
                )
                if passed:
                    deployed.append(node)
                else:
                    failures.append(node["id"])
                if manifest["strategy"] == "serial" and failures:
                    break
            if len(failures) > failure_threshold or (manifest["strategy"] == "serial" and failures):
                for node in reversed(deployed):
                    stage(
                        f"rollback:{node['id']}",
                        harness_command("deploy", "rollback", "--env", manifest["env"]),
                        {"HARNESS_DEPLOY_TARGET": node["deploy_target"], "HARNESS_DEPLOY_NODE_ID": node["id"]},
                    )
                raise RuntimeError(f"deployment failure threshold exceeded: {','.join(failures)}")
        manifest["deployment_result"] = {"deployed": [item["id"] for item in deployed], "failed": failures}
        if manifest["strategy"] == "blue-green":
            adapter = manifest["strategy_config"]["traffic_switch_adapter"]
            if not stage(
                "traffic-switch", [*shlex.split(adapter), "--env", manifest["env"], "--run-id", manifest["run_id"]]
            ):
                raise RuntimeError("blue-green traffic switch failed")
        if not stage("log-metric-evidence", harness_command("observability-check", "validate")):
            raise RuntimeError("observability evidence failed")
        if (
            manifest["env"] == "prod"
            and manifest.get("watch_window") != "0m"
            and not stage(
                "watch",
                [
                    *harness_command("deploy", "watch"),
                    "--env",
                    manifest["env"],
                    "--tag",
                    tag,
                    "--window",
                    manifest["watch_window"],
                ],
            )
        ):
            raise RuntimeError("production watch failed")
        result = {
            **manifest,
            "status": "planned" if dry_run else "passed",
            "stages": stages,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if not dry_run and manifest["env"] == "prod":
            store.write_json("stable/prod.json", result)
        return result
    except Exception as exc:
        return {
            **manifest,
            "status": "failed",
            "error": str(exc),
            "stages": stages,
            "finished_at": datetime.now(UTC).isoformat(),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "resume", "status", "rollback"))
    parser.add_argument("--env")
    parser.add_argument("--target", default="all")
    parser.add_argument("--profile", default="release")
    parser.add_argument("--from-run-id")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    context = HarnessContext.load()
    root = context.root
    store = context.pipeline_state
    try:
        manifest = (
            store.read_json(f"runs/{args.run_id}.json")
            if args.run_id
            else plan_pipeline(vars(args), dict(context.deploy), store, root)
        )
        if manifest is None:
            raise FileNotFoundError(f"run manifest not found: {args.run_id}")
        if args.command in {"run", "resume"}:
            manifest = run_pipeline(manifest, root, store, args.dry_run)
        elif args.command == "rollback":
            outcome = execute(
                CommandSpec.argv_command(
                    [
                        *harness_command("deploy", "rollback"),
                        "--env",
                        manifest["env"],
                    ],
                    cwd=root,
                )
            )
            manifest.update(
                {
                    "status": "rolled-back" if outcome.ok else "rollback-failed",
                    "rolled_back_at": datetime.now(UTC).isoformat(),
                }
            )
        store.write_json(f"runs/{manifest['run_id']}.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1 if manifest["status"] in {"failed", "rollback-failed"} else 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

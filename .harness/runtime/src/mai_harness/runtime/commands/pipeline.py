#!/usr/bin/env python3
"""Platform-neutral local delivery pipeline state machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.migration_progress import (
    begin,
    commit_checkpoint,
    complete,
    load_progress,
    record_failure,
)
from mai_harness.runtime.application.release_authorization import consume_release_rollback_authorization
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
    progress_id = f"pipeline.{manifest['run_id']}"
    resuming_finalization = manifest.get("status") == "finalizing"

    def persist(status: str = "running") -> None:
        if not dry_run:
            manifest.update({"status": status, "stages": stages, "updated_at": datetime.now(UTC).isoformat()})
            store.write_json(f"runs/{manifest['run_id']}.json", manifest)

    def stage(name: str, argv: list[str], environment: dict[str, str] | None = None) -> bool:
        spec = {"name": name, "command": argv, "env": environment or {}}
        input_hash = stage_input_hash(spec, manifest)
        if previous.get(name, {}).get("input_hash") == input_hash:
            stages.append({**previous[name], "resumed_skip": True})
            persist()
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
        persist()
        return dry_run or bool(outcome and outcome.ok)

    def reconcile_stage(name: str, argv: list[str], environment: dict[str, str]) -> None:
        spec = {"name": name, "command": argv, "env": environment}
        stages.append(
            {
                "name": name,
                "command": argv,
                "input_hash": stage_input_hash(spec, manifest),
                "status": "passed",
                "reconciled_from_deploy_receipt": True,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        persist()

    commands = [
        ("validate-inputs", harness_command("env-check", "validate")),
        ("project-tests", harness_command("run-project-command", "unit")),
    ]
    progress_started = False
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
        candidate_ref = (
            "sha256:" + hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        )
        if not dry_run and manifest["env"] == "prod":
            required = ["deploy-batches-complete"] + ([] if manifest.get("watch_window") == "0m" else ["watch-default"])
            progress = begin(
                root,
                progress_id,
                candidate_ref,
                workflow_kind="pipeline-release",
                required_checkpoints=required,
            )
            if progress.get("status") == "completed" and resuming_finalization:
                result = {
                    **manifest,
                    "status": "passed",
                    "stages": stages,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
                store.write_json("stable/prod.json", result)
                store.write_json(f"runs/{manifest['run_id']}.json", result)
                return result
            if progress.get("status") != "active":
                raise RuntimeError("Production checkpoint 状态不是 active；修复后先执行 migration-progress resume")
            progress_started = True
        batch_size = 1 if manifest["strategy"] == "serial" else int(manifest["strategy_config"].get("batch_size", 1))
        failure_threshold = int(manifest["strategy_config"].get("failure_threshold", 0))
        deployed: list[dict[str, Any]] = []
        failures: list[str] = []
        nodes = manifest["nodes"]
        if not 0 <= failure_threshold < len(nodes):
            raise RuntimeError(f"failure_threshold 必须满足 0 <= value < node count ({len(nodes)})")
        for offset in range(0, len(nodes), batch_size):
            batch = nodes[offset : offset + batch_size]
            for node in batch:
                env = {"HARNESS_DEPLOY_TARGET": node["deploy_target"], "HARNESS_DEPLOY_NODE_ID": node["id"]}
                deploy_argv = harness_command(
                    "deploy",
                    "--env",
                    manifest["env"],
                    "--tag",
                    tag or "<verified-tag>",
                    "--candidate-ref",
                    candidate_ref,
                    *(["--progress-id", progress_id] if manifest["env"] == "prod" else []),
                    *(["--defer-progress-failure"] if manifest["env"] == "prod" else []),
                    *[
                        value
                        for item in evidence
                        if item.get("artifact") and item.get("artifact_sha256")
                        for value in (
                            "--artifact",
                            f"{item['target']}={item['artifact']}",
                            "--artifact-sha256",
                            f"{item['target']}={item['artifact_sha256']}",
                        )
                    ],
                    *[
                        value
                        for item in evidence
                        if item.get("delivery") == "registry" and item.get("digest")
                        for value in ("--registry-digest", f"{item['target']}={item['digest']}")
                    ],
                )
                node_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(node["id"]))
                node_state = StateStore(root / ".harness/state").read_json(
                    f"deploy-{manifest['env']}-{node_key}.json", {}
                )
                if (
                    not dry_run
                    and node_state.get("action") == "deploy"
                    and node_state.get("success") is True
                    and node_state.get("tag") == tag
                    and node_state.get("progress_id") == progress_id
                    and node_state.get("candidate_ref") == candidate_ref
                    and node_state.get("node_id") == node_key
                    and node_state.get("deploy_target") == node["deploy_target"]
                ):
                    reconcile_stage(f"deploy:{node['id']}", deploy_argv, env)
                    passed = True
                else:
                    passed = stage(f"deploy:{node['id']}", deploy_argv, env)
                if passed:
                    deployed.append(node)
                else:
                    failures.append(node["id"])
                manifest["deployment_result"] = {
                    "deployed": [item["id"] for item in deployed],
                    "failed": failures,
                    "candidate_preserved": bool(failures),
                }
                persist()
                if manifest["strategy"] == "serial" and failures:
                    break
            if len(failures) > failure_threshold or (manifest["strategy"] == "serial" and failures):
                manifest["deployment_result"] = {
                    "deployed": [item["id"] for item in deployed],
                    "failed": failures,
                    "candidate_preserved": True,
                }
                raise RuntimeError(
                    f"deployment failure threshold exceeded: {','.join(failures)}; "
                    "candidate preserved，修复后 resume；不自动回滚"
                )
        manifest["deployment_result"] = {"deployed": [item["id"] for item in deployed], "failed": failures}
        if not deployed:
            raise RuntimeError("Production deployment 至少需要一个节点成功，禁止以旧服务健康状态完成发布")
        if not dry_run and manifest["env"] == "prod":
            commit_checkpoint(
                root,
                progress_id,
                "deploy-batches-complete",
                f"deployment batches completed; failed={','.join(failures) or 'none'}",
            )
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
                    "--progress-id",
                    progress_id,
                    "--required-progress-checkpoint",
                    "deploy-batches-complete",
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
            manifest.update({"status": "finalizing", "stages": stages, "finished_at": result["finished_at"]})
            persist("finalizing")
            complete(root, progress_id)
            result["status"] = "passed"
            store.write_json("stable/prod.json", result)
            store.write_json(f"runs/{manifest['run_id']}.json", result)
        return result
    except Exception as exc:
        if progress_started:
            try:
                progress_state = load_progress(root, progress_id)
                if progress_state.get("status") == "completed":
                    reconciled = {
                        **manifest,
                        "status": "passed",
                        "stages": stages,
                        "finished_at": datetime.now(UTC).isoformat(),
                        "reconciled": True,
                    }
                    store.write_json("stable/prod.json", reconciled)
                    store.write_json(f"runs/{manifest['run_id']}.json", reconciled)
                    return reconciled
                if progress_state.get("status") == "active":
                    record_failure(root, progress_id, "release", str(exc)[:1000])
            except ValueError:
                pass
        failed_result = {
            **manifest,
            "status": "failed",
            "error": str(exc),
            "stages": stages,
        }
        if not dry_run:
            store.write_json(f"runs/{manifest['run_id']}.json", failed_result)
        return failed_result


def rollback_pipeline(
    manifest: dict[str, Any], root: Path, authorization: Path, store: StateStore | None = None
) -> dict[str, Any]:
    """Rollback exactly the nodes recorded by one Pipeline run after explicit authorization."""
    if manifest.get("status") == "rolled-back":
        raise ValueError("pipeline 已完成回滚，禁止重复执行")
    evidence = manifest.get("artifact_evidence") or []
    candidate_ref = str(evidence[0].get("ref", "")).rsplit(":", 1)[-1] if evidence else ""
    consumer_id = f"pipeline.{hashlib.sha256(str(manifest['run_id']).encode()).hexdigest()[:24]}"
    authorization_record = consume_release_rollback_authorization(
        authorization,
        str(manifest["run_id"]),
        candidate_ref,
        root / ".harness/state",
        consumer_id,
    )
    deployed_ids = set((manifest.get("deployment_result") or {}).get("deployed", []))
    nodes = [node for node in manifest.get("nodes", []) if node.get("id") in deployed_ids]
    if not nodes:
        raise ValueError("pipeline 没有记录可回滚的已部署节点")
    rollback_results = list(manifest.get("rollback_results") or [])
    completed_nodes = {item.get("node") for item in rollback_results if item.get("returncode") == 0}
    outcome = None
    for node in reversed(nodes):
        if node["id"] in completed_nodes:
            continue
        node_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(node["id"]))
        deployed_state = StateStore(root / ".harness/state").read_json(f"deploy-{manifest['env']}-{node_key}.json", {})
        if (
            deployed_state.get("action") == "rollback"
            and deployed_state.get("success") is True
            and (deployed_state.get("authorization") or {}).get("receipt_sha256")
            == authorization_record.get("receipt_sha256")
        ):
            rollback_results.append({"node": node["id"], "returncode": 0, "reconciled": True})
            completed_nodes.add(node["id"])
            if store is not None:
                store.write_json(
                    f"runs/{manifest['run_id']}.json",
                    {**manifest, "status": "rollback-in-progress", "rollback_results": rollback_results},
                )
            continue
        outcome = execute(
            CommandSpec.argv_command(
                [
                    *harness_command("deploy", "rollback"),
                    "--env",
                    manifest["env"],
                    "--authorization",
                    str(authorization),
                    "--authorization-target",
                    str(manifest["run_id"]),
                    "--authorization-consumer",
                    consumer_id,
                ],
                cwd=root,
                env={
                    "HARNESS_DEPLOY_TARGET": node["deploy_target"],
                    "HARNESS_DEPLOY_NODE_ID": node["id"],
                },
            )
        )
        rollback_results.append({"node": node["id"], "returncode": outcome.returncode})
        checkpoint = {
            **manifest,
            "status": "rollback-in-progress" if outcome.ok else "rollback-failed",
            "rollback_results": rollback_results,
        }
        if store is not None:
            store.write_json(f"runs/{manifest['run_id']}.json", checkpoint)
        if not outcome.ok:
            break
    successful_nodes = {item.get("node") for item in rollback_results if item.get("returncode") == 0}
    return {
        **manifest,
        "status": ("rolled-back" if successful_nodes == {node["id"] for node in nodes} else "rollback-failed"),
        "rolled_back_at": datetime.now(UTC).isoformat(),
        "rollback_results": rollback_results,
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
    parser.add_argument("--authorization", type=Path)
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
            if args.authorization is None:
                raise ValueError("pipeline rollback 必须提供人员明确确认的 --authorization 文件")
            manifest = rollback_pipeline(manifest, root, args.authorization, store)
        store.write_json(f"runs/{manifest['run_id']}.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1 if manifest["status"] in {"failed", "rollback-failed"} else 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

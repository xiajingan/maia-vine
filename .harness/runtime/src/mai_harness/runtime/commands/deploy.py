#!/usr/bin/env python3
"""Deploy immutable image tags through local or SSH Docker Compose."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from mai_harness.runtime.application.integration_contract import validate_deploy_inputs
from mai_harness.runtime.application.migration_progress import (
    commit_checkpoint,
    load_progress,
    record_failure,
)
from mai_harness.runtime.application.promotion_preflight import (
    promotion_input_identity,
    validate_promotion_preflight,
)
from mai_harness.runtime.application.release_authorization import consume_release_rollback_authorization
from mai_harness.runtime.application.required_secrets import resolve_required_secrets
from mai_harness.runtime.application.task_evidence import current_attempt_state_path
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import (
    DeployAdapter,
    get_deploy_adapter,
    get_environment,
    load_deploy_config,
)
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.secrets_file import load_secrets_file_snapshot


def environment_values(name: str, entry: dict) -> tuple[dict[str, str], list[str]]:
    required = resolve_required_secrets(name, entry)
    snapshot = load_secrets_file_snapshot(name, required)
    values = {**os.environ, **snapshot["values"]}
    missing = [
        key
        for key in required
        if not values.get(key) and not (key.endswith("_SSH_PRIVATE_KEY") and values.get("HARNESS_SSH_KEY_PATH"))
    ]
    return values, missing + ([f"secrets file error: {snapshot['error']}"] if snapshot["error"] else [])


def expand(value: str, values: dict[str, str]) -> str:
    return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda match: values.get(match.group(1), match.group(0)), value)


def plan(name: str, tag: str, entry: dict, values: dict[str, str]) -> dict:
    target = expand(os.environ.get("HARNESS_DEPLOY_TARGET", entry["deploy_target"]), values)
    parsed = urlparse(target)
    return {
        "env": name,
        "tag": tag,
        "target": target,
        "remote": parsed.scheme == "ssh",
        "ssh_host": parsed.hostname,
        "ssh_user": parsed.username,
        "ssh_port": parsed.port or 22,
        "workdir": expand(os.environ.get("HARNESS_REMOTE_WORKDIR", entry["remote_workdir"]), values),
        "compose_file": entry["compose_file"],
        "remote_compose_file": entry.get("remote_compose_file", entry["compose_file"]),
        "health_url": expand(entry["health_url"], values),
        "smoke_cmd": expand(entry.get("smoke_cmd", ""), values),
        "node_id": os.environ.get("HARNESS_DEPLOY_NODE_ID", "default"),
    }


def health(url: str, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=10) as response:
                if response.status < 400:
                    return True
        except (URLError, TimeoutError):
            pass
        time.sleep(2)
    return False


def execute_deploy(deployment: dict, dry_run: bool, artifacts: list[dict[str, str]] | None = None) -> bool:
    env = {"IMAGE_TAG": deployment["tag"]}
    artifacts = artifacts or []
    if deployment["remote"]:
        destination = f"{deployment['ssh_user']}@{deployment['ssh_host']}"
        ssh = ["ssh", "-p", str(deployment["ssh_port"]), destination]
        commands = [
            [
                "scp",
                "-P",
                str(deployment["ssh_port"]),
                deployment["compose_file"],
                f"{destination}:{deployment['workdir']}/{deployment['remote_compose_file']}",
            ],
            *[
                [
                    "scp",
                    "-P",
                    str(deployment["ssh_port"]),
                    artifact["path"],
                    f"{destination}:{deployment['workdir']}/.harness-{artifact['name']}.tar",
                ]
                for artifact in artifacts
            ],
            *[
                [
                    *ssh,
                    f"docker load --input {deployment['workdir']}/.harness-{artifact['name']}.tar",
                ]
                for artifact in artifacts
            ],
            [
                *ssh,
                f"cd {deployment['workdir']} && IMAGE_TAG={deployment['tag']} docker compose -f {deployment['remote_compose_file']} up -d --wait",
            ],
        ]
    else:
        commands = [
            *[["docker", "load", "--input", artifact["path"]] for artifact in artifacts],
            ["docker", "compose", "-f", deployment["compose_file"], "up", "-d", "--wait"],
        ]
    for command in commands:
        print(" ".join(command))
        if not dry_run:
            outcome = execute(CommandSpec.argv_command(command, cwd=Path.cwd(), env=env))
            if not outcome.ok:
                print(f"❌ {outcome.stderr or outcome.stdout}")
                return False
    if dry_run:
        return True
    if not health(deployment["health_url"]):
        return False
    smoke = deployment.get("smoke_cmd", "").replace("$URL", deployment["health_url"].removesuffix("/health"))
    return not smoke or execute(CommandSpec.argv_command(shlex.split(smoke), cwd=Path.cwd(), env=env)).ok


IMMUTABLE_TAG = re.compile(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{7,64})\Z")
SHA256 = re.compile(r"sha256:([0-9a-f]{64})\Z")


def parse_artifacts(paths: list[str], digests: list[str]) -> list[dict[str, str]]:
    """Validate named local artifacts without reading secret-like values."""
    parsed_paths = dict(item.split("=", 1) for item in paths if "=" in item)
    parsed_digests = dict(item.split("=", 1) for item in digests if "=" in item)
    if not paths and not digests:
        return []
    if len(parsed_paths) != len(paths) or parsed_paths.keys() != parsed_digests.keys() or not parsed_paths:
        raise ValueError("adapter deploy 要求匹配的 --artifact NAME=PATH 与 --artifact-sha256 NAME=sha256:...")
    output: list[dict[str, str]] = []
    for name, value in sorted(parsed_paths.items()):
        expected = parsed_digests[name]
        match = SHA256.fullmatch(expected)
        path = Path(value)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not match or not path.is_file():
            raise ValueError(f"artifact {name} 的名称、文件或 SHA-256 无效")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != match.group(1):
            raise ValueError(f"artifact {name} SHA-256 不匹配")
        output.append({"name": name, "path": str(path), "sha256": expected})
    return output


def promote_task_binding(root: Path, sprint_id: str, task_id: str) -> dict[str, object]:
    """Resolve a deployment invocation to the current ready promote-test attempt."""

    sprint = next(
        (
            path
            for directory in ("active", "completed")
            if (path := root / "docs/exec-plans" / directory / f"{Path(sprint_id).name}.md").is_file()
        ),
        None,
    )
    if sprint is None:
        raise ValueError(f"Sprint 计划不存在: {sprint_id}")
    path = current_attempt_state_path(root, sprint, task_id)
    try:
        attempt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"promote-test attempt 无法读取: {path}: {exc}") from exc
    if (
        attempt.get("schema_version") != 3
        or attempt.get("sprint") != sprint.stem
        or attempt.get("task_id") != task_id
        or attempt.get("task_type") != "promote-test"
        or attempt.get("status") != "ready"
        or attempt.get("review") is not None
    ):
        raise ValueError("deploy 必须绑定当前 ready 且尚未 Review 的 promote-test attempt")
    return {
        "sprint": sprint.stem,
        "task_id": task_id,
        "attempt_run_id": attempt["run_id"],
        "attempt": attempt["attempt"],
    }


def adapter_plan(
    name: str,
    action: str,
    tag: str,
    adapter: DeployAdapter,
    artifacts: list[dict[str, str]],
    secrets_file: str,
    registry_digests: list[str] | None = None,
) -> dict:
    # Preflight only validates and renders the adapter invocation; it never
    # executes it, so adapters must not need to expose a non-mutation action.
    if action != "preflight" and action not in adapter.actions:
        raise ValueError(f"deploy adapter 不允许 action={action}")
    if not IMMUTABLE_TAG.fullmatch(tag):
        raise ValueError("deploy adapter 要求不可变 commit/digest tag")
    argv = [*adapter.argv, "--environment", name, "--action", action, "--tag", tag, "--secrets-file", secrets_file]
    for artifact in artifacts:
        argv.extend(["--artifact", f"{artifact['name']}={artifact['path']}"])
        argv.extend(["--artifact-sha256", f"{artifact['name']}={artifact['sha256']}"])
    for digest in registry_digests or []:
        argv.extend(["--registry-digest", digest])
    digest = hashlib.sha256(
        json.dumps(
            {"action": action, "tag": tag, "artifacts": artifacts, "registry_digests": registry_digests or []},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {"env": name, "action": action, "tag": tag, "adapter": adapter.argv[0], "argv": argv, "input_sha256": digest}


def execute_adapter(deployment: dict, dry_run: bool) -> bool:
    visible_argv = list(deployment["argv"])
    if "--secrets-file" in visible_argv:
        visible_argv[visible_argv.index("--secrets-file") + 1] = "<protected-path>"
    print(json.dumps({**deployment, "argv": visible_argv}, ensure_ascii=False))
    if dry_run:
        return True
    return execute(CommandSpec.argv_command(deployment["argv"], cwd=Path.cwd())).ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", default="deploy", choices=("deploy", "preflight-secrets", "preflight", "watch", "rollback")
    )
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    parser.add_argument("--tag")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--artifact-sha256", action="append", default=[])
    parser.add_argument("--registry-digest", action="append", default=[])
    parser.add_argument("--sprint")
    parser.add_argument("--task-id")
    parser.add_argument("--window", default="30m")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--authorization-target", help=argparse.SUPPRESS)
    parser.add_argument("--authorization-consumer", help=argparse.SUPPRESS)
    parser.add_argument("--progress-id")
    parser.add_argument("--defer-progress-failure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--required-progress-checkpoint", help=argparse.SUPPRESS)
    parser.add_argument("--candidate-ref", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path.cwd()
    store = StateStore(root / ".harness/state")
    if bool(args.sprint) != bool(args.task_id):
        parser.error("--sprint 与 --task-id 必须同时提供")
    try:
        task_binding = promote_task_binding(root, args.sprint, args.task_id) if args.sprint else {}
    except ValueError as exc:
        parser.error(str(exc))
    try:
        entry = get_environment(args.env)
        adapter = get_deploy_adapter(entry)
        values, missing = ({}, []) if adapter and args.dry_run else environment_values(args.env, entry)
        deploy_config = load_deploy_config() if task_binding else {}
        harness_config = load_harness_config() if task_binding else {}
    except (OSError, ValueError, KeyError) as exc:
        print(f"❌ {exc}")
        return 1
    if args.action == "preflight-secrets":
        for item in missing:
            print(f"❌ {item}")
        return 1 if missing else 0
    if missing:
        for item in missing:
            print(f"❌ {item}")
        return 1
    requested_node = re.sub(r"[^A-Za-z0-9_.-]", "_", os.environ.get("HARNESS_DEPLOY_NODE_ID", "default"))
    previous = store.read_json(f"deploy-{args.env}-{requested_node}.json", {})
    progress_id = args.progress_id
    progress_state = None
    if args.env == "prod" and args.action in {"deploy", "watch"}:
        if not progress_id:
            parser.error("Production deploy/watch 必须提供 --progress-id 并绑定 checkpoint 状态")
        try:
            progress_state = load_progress(root, progress_id)
        except ValueError as exc:
            parser.error(str(exc))
        expected_candidate = str(args.candidate_ref or args.tag or previous.get("tag", ""))
        if progress_state.get("status") != "active":
            parser.error("Production checkpoint 状态不是 active；修复后先执行 migration-progress resume")
        if progress_state.get("candidate_ref") != expected_candidate:
            parser.error("Production checkpoint candidate_ref 与部署 tag 不一致")
        if progress_state.get("workflow_kind") not in {"pipeline-release", "manual-release"}:
            parser.error("Production deploy/watch 必须绑定 release workflow，不能复用业务 migration 进度")
        checkpoint_ids = {item.get("id") for item in progress_state.get("committed_checkpoints", [])}
        if args.action == "watch":
            watch_checkpoint = f"watch-{requested_node}"
            graph = progress_state.get("required_checkpoints", [])
            if watch_checkpoint not in graph:
                parser.error(f"Production workflow 未声明 required checkpoint: {watch_checkpoint}")
            prior = graph[: graph.index(watch_checkpoint)]
            required_checkpoint = args.required_progress_checkpoint or (prior[-1] if prior else "")
            if not required_checkpoint or required_checkpoint not in prior or required_checkpoint not in checkpoint_ids:
                parser.error(f"Production watch 缺少 graph 前置 checkpoint: {prior}")
    authorization = None
    if args.action == "rollback":
        try:
            current_tag = str(previous.get("tag", ""))
            authorization_target = args.authorization_target or current_tag
            authorization = consume_release_rollback_authorization(
                args.authorization,
                authorization_target,
                current_tag,
                root / ".harness/state",
                args.authorization_consumer
                or f"deploy.{args.env}.{requested_node}.{hashlib.sha256(authorization_target.encode()).hexdigest()[:16]}",
            )
        except ValueError as exc:
            parser.error(str(exc))
    tag = args.tag or (previous.get("previous_tag") if args.action == "rollback" else previous.get("tag"))
    if not tag:
        parser.error("--tag 必填，且无可回滚状态")
    try:
        artifacts = (
            parse_artifacts(args.artifact, args.artifact_sha256) if args.action in {"deploy", "preflight"} else []
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    registry_digests = sorted(args.registry_digest)
    if any(not SHA256.fullmatch(value) for value in registry_digests):
        print("❌ --registry-digest 必须是 sha256:<64 hex>")
        return 1
    if artifacts and registry_digests:
        print("❌ 单次 deploy 只能选择 artifact 或 registry digest 一种交付身份")
        return 1
    if not adapter and registry_digests:
        print("❌ 内置 Docker Compose 部署不能证明 registry digest；请使用 deploy adapter")
        return 1
    preflight_receipt: dict[str, object] = {}
    if task_binding and args.action in {"deploy", "preflight"}:
        _, identity_errors = validate_deploy_inputs(
            root,
            str(task_binding["sprint"]),
            str(args.tag or ""),
            artifacts,
            registry_digests,
        )
        if identity_errors:
            for error in identity_errors:
                print(f"❌ {error}")
            return 1
    if task_binding and args.action == "deploy" and not args.dry_run:
        input_identity, preflight_errors = promotion_input_identity(
            root,
            args.env,
            deploy_config,
            harness_config,
            entry,
            values,
        )
        preflight_receipt, receipt_errors = validate_promotion_preflight(
            root,
            args.env,
            task_binding,
            expected_identity=input_identity,
        )
        preflight_errors.extend(receipt_errors)
        if preflight_errors:
            for error in preflight_errors:
                print(f"❌ {error}")
            return 1
    if adapter:
        try:
            deployment = adapter_plan(
                args.env,
                args.action,
                tag,
                adapter,
                artifacts,
                entry["secrets_source"],
                registry_digests,
            )
        except ValueError as exc:
            print(f"❌ {exc}")
            return 1
    else:
        deployment = plan(args.env, tag, entry, values)
    if args.action == "preflight":
        print(json.dumps(deployment, ensure_ascii=False, indent=2))
        return 0
    if args.action == "watch" and not adapter:
        watched = health(
            deployment["health_url"],
            int(re.match(r"\d+", args.window).group()) * (60 if args.window.endswith("m") else 1),
        )
        if progress_id:
            if watched:
                commit_checkpoint(root, progress_id, f"watch-{requested_node}", f"{args.env} health window passed")
            else:
                record_failure(root, progress_id, "service-start", "health window failed")
        return 0 if watched else 1
    if args.action == "watch" and adapter:
        watched = execute_adapter(deployment, args.dry_run)
        if progress_id and not args.dry_run:
            if watched:
                commit_checkpoint(root, progress_id, f"watch-{requested_node}", f"{args.env} health window passed")
            else:
                record_failure(root, progress_id, "service-start", "health window failed")
        return 0 if watched else 1
    if adapter and args.dry_run:
        execute_adapter(deployment, True)
        return 0
    success = (
        execute_adapter(deployment, args.dry_run) if adapter else execute_deploy(deployment, args.dry_run, artifacts)
    )
    state_deployment = (
        {key: deployment[key] for key in ("env", "action", "tag", "adapter", "input_sha256")} if adapter else deployment
    )
    attempted_tag = str(deployment["tag"])
    last_stable_tag = str(
        previous.get("stable_tag")
        or (previous.get("tag") if previous.get("success") else previous.get("previous_tag"))
        or ""
    )
    current_tag = attempted_tag if success else last_stable_tag
    state = {
        **state_deployment,
        "action": args.action,
        "tag": current_tag,
        "attempted_tag": attempted_tag,
        "success": success,
        "previous_tag": last_stable_tag if success else str(previous.get("previous_tag", "")),
        "stable_tag": current_tag if success else last_stable_tag,
        "progress_id": progress_id or "",
        "candidate_ref": str(args.candidate_ref or attempted_tag),
        "node_id": requested_node,
        "deploy_target": str(deployment.get("target", os.environ.get("HARNESS_DEPLOY_TARGET", ""))),
        "task_binding": task_binding,
        **({"preflight_receipt": preflight_receipt} if task_binding else {}),
        "artifacts": sorted(
            [{"name": item["name"], "sha256": item["sha256"]} for item in artifacts],
            key=lambda item: item["name"],
        ),
        "registry_digests": registry_digests,
        "readiness": "passed" if success else "failed",
        "smoke": "passed" if success else "failed",
        "ts": datetime.now(UTC).isoformat(),
        **({"authorization": authorization} if authorization else {}),
    }
    node = re.sub(r"[^A-Za-z0-9_.-]", "_", deployment.get("node_id", requested_node))
    store.write_json(f"deploy-{args.env}-{node}.json", state)
    if progress_id:
        if success:
            checkpoint_id = f"watch-{node}" if args.action == "watch" else f"deploy-{node}"
            commit_checkpoint(root, progress_id, checkpoint_id, f"{args.env} {args.action} passed")
        elif not args.defer_progress_failure:
            record_failure(root, progress_id, "release", f"deploy failed on {node}")
    print(f"{'✅' if success else '❌'} deploy {args.env} tag={tag}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

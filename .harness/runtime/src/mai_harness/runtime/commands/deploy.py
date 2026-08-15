#!/usr/bin/env python3
"""Deploy immutable image tags through local or SSH Docker Compose."""

from __future__ import annotations

import argparse
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

from mai_harness.runtime.application.required_secrets import resolve_required_secrets
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import get_environment
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


def execute_deploy(deployment: dict, dry_run: bool) -> bool:
    env = {"IMAGE_TAG": deployment["tag"]}
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
            [
                *ssh,
                f"cd {deployment['workdir']} && IMAGE_TAG={deployment['tag']} docker compose -f {deployment['remote_compose_file']} up -d --wait",
            ],
        ]
    else:
        commands = [["docker", "compose", "-f", deployment["compose_file"], "up", "-d", "--wait"]]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", default="deploy", choices=("deploy", "preflight-secrets", "preflight", "watch", "rollback")
    )
    parser.add_argument("--env", required=True, choices=("test", "prod"))
    parser.add_argument("--tag")
    parser.add_argument("--window", default="30m")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    store = StateStore(root / ".harness/state")
    try:
        entry = get_environment(args.env)
        values, missing = environment_values(args.env, entry)
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
    tag = args.tag or (previous.get("previous_tag") if args.action == "rollback" else previous.get("tag"))
    if not tag:
        parser.error("--tag 必填，且无可回滚状态")
    deployment = plan(args.env, tag, entry, values)
    if args.action == "preflight":
        print(json.dumps(deployment, ensure_ascii=False, indent=2))
        return 0
    if args.action == "watch":
        return (
            0
            if health(
                deployment["health_url"],
                int(re.match(r"\d+", args.window).group()) * (60 if args.window.endswith("m") else 1),
            )
            else 1
        )
    success = execute_deploy(deployment, args.dry_run)
    state = {
        **deployment,
        "success": success,
        "previous_tag": previous.get("tag", ""),
        "readiness": "passed" if success else "failed",
        "smoke": "passed" if success else "failed",
        "ts": datetime.now(UTC).isoformat(),
    }
    node = re.sub(r"[^A-Za-z0-9_.-]", "_", deployment["node_id"])
    store.write_json(f"deploy-{args.env}-{node}.json", state)
    print(f"{'✅' if success else '❌'} deploy {args.env} tag={tag}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Render or execute policy-bound Helm deployments for Control releases."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.adapters import HelmAdapter, execute_helm
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.manifest import load_manifest, validate_release
from mai_harness.runtime.infrastructure.utils import run_capture


def deploy_release(
    manifest_path: Path,
    environment: str,
    *,
    execute: bool,
    production_authorized: bool,
    baseline_manifest: Path | None = None,
) -> dict[str, Any]:
    """Deploy a release; production execution is reserved for the transition use case."""
    config = load_harness_config()
    if config["project"]["mode"] != "control":
        raise ValueError("kubernetes 命令仅允许 control 模式")
    if execute and environment == "prod" and not production_authorized:
        raise ValueError("Production 执行只能通过 control release-promote/release-rollback")
    manifest = manifest_path.resolve()
    release_root = (PATHS.state / "releases").resolve()
    if not manifest.is_file() or manifest.parent != release_root:
        raise ValueError("仅允许部署 .harness/state/releases 下的 Release Manifest")
    release = load_manifest(manifest)
    if errors := validate_release(release):
        raise ValueError("Release 校验失败: " + "; ".join(errors))
    charts = [
        (delivery["project_id"], artifact)
        for delivery in release["deliveries"]
        for artifact in delivery.get("artifacts", [])
        if artifact.get("type") == "helm-chart"
    ]
    if not charts:
        raise ValueError("Release 至少包含一个 helm-chart 制品")
    policy = config.get("control", {}).get("kubernetes", {}).get("environments", {}).get(environment, {})
    if any(not policy.get(field) for field in ("context", "cluster", "namespace")):
        raise ValueError(f"control.kubernetes.environments.{environment} 必须配置 context/cluster/namespace")
    kubectl = ["kubectl"]
    if policy.get("kubeconfig"):
        kubectl.extend(("--kubeconfig", policy["kubeconfig"]))
    observed_context = run_capture([*kubectl, "config", "current-context"]).strip()
    observed_cluster = run_capture(
        [*kubectl, "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]
    ).strip()
    adapter = HelmAdapter(policy["context"], policy["cluster"], policy["namespace"], policy.get("kubeconfig", ""))
    adapter.verify_identity(observed_context=observed_context, observed_cluster=observed_cluster)
    commands = []
    target_ids = {chart["deployment_id"] for _, chart in charts}
    for _project_id, chart in charts:
        if not str(chart["ref"]).endswith("@" + chart["digest"]):
            raise ValueError("helm-chart ref 必须以 @sha256:digest 固定内容")
        command = adapter.apply_chart(
            release=chart["deployment_id"],
            chart=chart["ref"],
            version=chart["version"],
            namespace=policy["namespace"],
            dry_run=not execute,
        )
        commands.append(command)
        if execute:
            execute_helm(command, cwd=PATHS.project)
    removed: list[str] = []
    if execute and environment == "prod":
        ledger = StateStore(PATHS.state / "releases").read_json("stable.json", {})
        current_manifest = str(baseline_manifest.resolve()) if baseline_manifest else (ledger.get("current") or {}).get("manifest")
        if current_manifest and Path(current_manifest).resolve() != manifest:
            current = load_manifest(Path(current_manifest))
            current_ids = {
                artifact["deployment_id"]
                for delivery in current.get("deliveries", [])
                for artifact in delivery.get("artifacts", [])
                if artifact.get("type") == "helm-chart"
            }
            for deployment_id in sorted(current_ids - target_ids):
                command = adapter.remove_chart(release=deployment_id, namespace=policy["namespace"])
                commands.append(command)
                execute_helm(command, cwd=PATHS.project)
                removed.append(deployment_id)
    evidence = None
    if execute:
        evidence = StateStore(PATHS.state / "deployments").write_json(
            f"{release['release_id']}-{environment}-{release['status']}-{uuid.uuid4().hex}.json",
            {
                "release": release["release_id"],
                "environment": environment,
                "manifest_digest": release["manifest_digest"],
                "commands": commands,
                "removed_deployments": removed,
                "deployment_identity": {
                    "context": policy["context"],
                    "cluster": policy["cluster"],
                    "namespace": policy["namespace"],
                },
                "producer": "mai-harness:control.test.deploy" if environment == "test" else "mai-harness:control.release",
                "status": "applied",
            },
        )
    return {"commands": commands, "evidence": str(evidence) if evidence else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--env", choices=("test", "prod"), required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = deploy_release(args.manifest, args.env, execute=args.execute, production_authorized=False)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

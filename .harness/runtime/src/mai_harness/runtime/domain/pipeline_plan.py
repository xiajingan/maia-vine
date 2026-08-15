"""Build immutable local Pipeline plans and safe-fix policy decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(value: Any) -> str:
    """Hash deterministic planning inputs without depending on stateful services."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def select_targets(
    config: dict[str, Any], requested: str = "all", profile: str = "release"
) -> list[tuple[str, dict[str, Any]]]:
    entries = list(config.get("build", {}).get("targets", {}).items())
    selected = entries if requested == "all" else [(name, value) for name, value in entries if name == requested]
    if not selected:
        raise ValueError(f"build target not found: {requested}")
    return [(name, value) for name, value in selected if not value.get("profiles") or profile in value["profiles"]]


def create_pipeline_plan(
    config: dict[str, Any], *, env: str, target: str = "all", profile: str = "release", source_sha: str
) -> dict[str, Any]:
    environment = config.get("environments", {}).get(env)
    if not environment:
        raise ValueError(f"environment not found: {env}")
    targets = [
        {
            "name": name,
            "type": value.get("type", "docker"),
            "dockerfile": value.get("dockerfile"),
            "context": value.get("context", "."),
            "platforms": value.get("platforms", [value.get("platform", config["build"]["default_platform"])]),
            "profile": profile,
            "delivery": value.get("delivery", environment.get("delivery_mode", "artifact")),
            "depends_on": value.get("depends_on", []),
        }
        for name, value in select_targets(config, target, profile)
    ]
    names = {item["name"] for item in targets}
    for item in targets:
        missing = set(item["depends_on"]) - names
        if missing:
            raise ValueError(f"target {item['name']} depends on missing target {next(iter(missing))}")
    raw_nodes = environment.get("targets") or (
        [environment.get("deploy_target")] if environment.get("deploy_target") else []
    )
    nodes = [
        {"id": f"{env}-{index + 1}", "deploy_target": item} if isinstance(item, str) else item
        for index, item in enumerate(raw_nodes)
    ]
    if not nodes:
        raise ValueError(f"environment has no deployment nodes: {env}")
    strategy = environment.get("strategy", "serial")
    if strategy not in {"serial", "rolling", "blue-green"}:
        raise ValueError(f"unsupported deployment strategy: {strategy}")
    if strategy == "blue-green" and not environment.get("traffic_switch_adapter"):
        raise ValueError("blue-green requires environments.<env>.traffic_switch_adapter")
    plan = {
        "version": 2,
        "env": env,
        "source_sha": source_sha,
        "baseline_id": config.get("baseline_id", "default"),
        "profile": profile,
        "strategy": strategy,
        "strategy_config": {
            "batch_size": environment.get("batch_size", 1),
            "failure_threshold": environment.get("failure_threshold", 0),
            "traffic_switch_adapter": environment.get("traffic_switch_adapter", ""),
        },
        "targets": targets,
        "nodes": nodes,
        "checks": environment.get(
            "checks", {"health_url": environment.get("health_url"), "smoke_cmd": environment.get("smoke_cmd")}
        ),
        "watch_window": f"{environment.get('health_window_minutes', 0)}m",
    }
    return {**plan, "config_hash": stable_hash(config), "plan_hash": stable_hash(plan)}


def stage_input_hash(stage: dict[str, Any], plan: dict[str, Any]) -> str:
    return stable_hash(
        {
            key: value
            for key, value in {
                "stage": stage["name"],
                "command": stage["command"],
                "env": stage.get("env", {}),
                "source_sha": plan["source_sha"],
                "config_hash": plan["config_hash"],
                "artifact_evidence": plan.get("artifact_evidence", []),
            }.items()
        }
    )


def can_autofix(policy: dict[str, Any], finding: dict[str, Any]) -> bool:
    return bool(
        policy.get("enabled")
        and policy.get("mode") == "safe-fix"
        and finding.get("job") in policy.get("allowed_jobs", [])
        and finding.get("severity") in policy.get("allowed_severities", [])
        and finding.get("attempts", 0) < policy.get("max_attempts", 0)
        and not any(
            path.startswith(prefix)
            for path in finding.get("touches", [])
            for prefix in policy.get("protected_paths", [])
        )
    )


def validate_artifact_evidence(images: list[dict[str, Any]], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {image["target"]: image for image in images}
    evidence = []
    for target in targets:
        image = indexed.get(target["name"])
        if not image:
            raise ValueError(f"missing artifact evidence for target: {target['name']}")
        artifact_sha = image.get("artifactSha256") or image.get("artifact_sha256", "")
        if target["delivery"] == "registry" and not image.get("digest"):
            raise ValueError(f"registry target requires digest: {target['name']}")
        if target["delivery"] == "artifact" and not artifact_sha:
            raise ValueError(f"artifact target requires SHA-256: {target['name']}")
        evidence.append(
            {
                "target": target["name"],
                "delivery": target["delivery"],
                "ref": image["ref"],
                "digest": image.get("digest", ""),
                "artifact": image.get("artifact", ""),
                "artifact_sha256": artifact_sha,
            }
        )
    return evidence

"""Managed Delivery Manifest publish port."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.collaboration import publish_delivery, validate_delivery_publication
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.manifest import (
    delivery_artifact_identities,
    digest,
    load_manifest,
    now,
    validate_delivery,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("manifest", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args()
    config = load_harness_config()
    if config["project"]["mode"] != "managed":
        parser.error("delivery 命令仅允许 managed 模式")
    if args.command == "publish":
        print(publish_delivery(PATHS.project, config["management"]["deliveries_dir"], load_manifest(args.manifest)))
        return 0
    delivery = load_manifest(args.manifest)
    if errors := validate_delivery(delivery):
        parser.error("Delivery 校验失败: " + "; ".join(errors))
    if errors := validate_delivery_publication(PATHS.project, args.manifest, delivery):
        parser.error("Delivery 发布登记校验失败: " + "; ".join(errors))
    commands = config["management"].get("supply_chain_verification_commands", [])
    if not commands:
        parser.error("delivery verify 必须配置 management.supply_chain_verification_commands")
    expected = delivery_artifact_identities(delivery)
    verifiers = []
    for index, command in enumerate(commands, start=1):
        argv = [value.replace("{manifest}", str(args.manifest.resolve())) for value in command]
        outcome = execute(CommandSpec.argv_command(argv, cwd=PATHS.project))
        if not outcome.ok:
            parser.error(f"供应链验证命令 {index} 失败: {outcome.stderr[-1000:]}")
        try:
            result = json.loads(outcome.stdout)
        except json.JSONDecodeError:
            parser.error(f"供应链验证命令 {index} 必须输出 JSON")
        if (
            not isinstance(result, dict)
            or result.get("manifest_digest") != delivery["manifest_digest"]
            or result.get("artifacts") != expected
            or not all(result.get(field) is True for field in ("signature", "sbom", "build_once"))
        ):
            parser.error(f"供应链验证命令 {index} 未验证当前 Delivery 的全部 Artifact")
        verifiers.append(
            {
                "argv": argv,
                "returncode": outcome.returncode,
                "signature": True,
                "sbom": True,
                "build_once": True,
            }
        )
    payload = {
        "status": "passed",
        "manifest_digest": delivery["manifest_digest"],
        "artifacts": expected,
        "verifiers": verifiers,
        "verified_at": now(),
    }
    receipt = {**payload, "receipt_digest": digest(payload)}
    target = StateStore(PATHS.state / "delivery-verifications").write_json(
        f"{delivery['manifest_digest'].removeprefix('sha256:')}.json", receipt
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Control-mode project, release, and integration commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mai_harness.runtime.application.control import (
    ControlAssignmentService,
    assert_global_delivery_ids,
    assert_registered_delivery_paths,
    assert_registered_delivery_states,
    complete_rollback,
    compose_release,
    load_managed_projects,
    promote_stable_release,
    resolve_rollback_release,
    transition_release,
    validate_registered_relationships,
)
from mai_harness.runtime.commands.kubernetes import deploy_release
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.manifest import load_manifest, validate_delivery, validate_release

_PRODUCTION_LOCKED = False


def main() -> int:
    global _PRODUCTION_LOCKED
    if not _PRODUCTION_LOCKED and len(sys.argv) > 1 and sys.argv[1] in {"release-promote", "release-rollback"}:
        with StateStore(PATHS.state / "release-operations").lock("production.environment", timeout_seconds=1):
            _PRODUCTION_LOCKED = True
            try:
                return main()
            finally:
                _PRODUCTION_LOCKED = False
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("managed-project-check")
    check.add_argument("--config", type=Path)
    dispatch = sub.add_parser("assignment-dispatch")
    dispatch.add_argument("manifest", type=Path)
    dispatch.add_argument("--config", type=Path)
    statuses = sub.add_parser("assignment-status")
    statuses.add_argument("--config", type=Path)
    verify = sub.add_parser("delivery-verify")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--config", type=Path)
    compose = sub.add_parser("release-compose")
    compose.add_argument("release_id")
    compose.add_argument("deliveries", nargs="+", type=Path)
    compose.add_argument("--config", type=Path)
    transition = sub.add_parser("release-transition")
    transition.add_argument("manifest", type=Path)
    transition.add_argument("status", choices=("test-verifying", "failed"))
    transition.add_argument("--evidence", action="append", default=[])
    for name in ("release-promote", "release-rollback"):
        operation = sub.add_parser(name)
        operation.add_argument("manifest", type=Path)
    finding = sub.add_parser("integration-finding")
    finding.add_argument("manifest", type=Path)
    finding.add_argument("--summary", required=True)
    finding.add_argument("--project", action="append", default=[])
    finding.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args()
    harness = load_harness_config()
    if harness["project"]["mode"] != "control":
        parser.error("control 命令仅允许 control 模式")
    managed_path = (
        Path(args.config) if getattr(args, "config", None) else PATHS.project / harness["control"]["managed_projects"]
    )
    if args.command == "managed-project-check":
        projects = load_managed_projects(managed_path)
        relationships = validate_registered_relationships(projects, PATHS.project, harness["project"]["id"])
        result = {"projects": len(projects), "relationships": relationships}
        target = PATHS.state / "control/managed-project-validation.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**result, "evidence": str(target)}, ensure_ascii=False))
    elif args.command == "assignment-dispatch":
        projects = load_managed_projects(managed_path)
        validate_registered_relationships(projects, PATHS.project, harness["project"]["id"])
        service = ControlAssignmentService(PATHS.state, projects, harness["project"]["id"])
        result = service.dispatch(load_manifest(args.manifest), PATHS.project)
        print(
            json.dumps(
                {key: str(value) if isinstance(value, Path) else value for key, value in result.items()},
                ensure_ascii=False,
            )
        )
    elif args.command == "assignment-status":
        projects = load_managed_projects(managed_path)
        validate_registered_relationships(projects, PATHS.project, harness["project"]["id"])
        result = ControlAssignmentService(PATHS.state, projects).status(PATHS.project)
        target = PATHS.state / "control/assignment-status.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"statuses": result, "evidence": str(target)}, ensure_ascii=False))
    elif args.command == "delivery-verify":
        projects = load_managed_projects(managed_path)
        assert_registered_delivery_paths([args.manifest], projects, PATHS.project)
        assert_registered_delivery_states([args.manifest], projects, PATHS.project)
        assert_global_delivery_ids(projects, PATHS.project)
        delivery = load_manifest(args.manifest)
        if errors := validate_delivery(delivery):
            parser.error("Delivery 校验失败: " + "; ".join(errors))
        supply_commands = harness.get("control", {}).get("supply_chain_verification_commands", [])
        if not supply_commands:
            parser.error("delivery-verify 必须配置 control.supply_chain_verification_commands")
        supply_evidence = []
        artifact_identities = [
            {
                "type": artifact["type"],
                "ref": artifact["ref"],
                "digest": artifact.get("digest") or "sha256:" + str(artifact.get("sha256", "")).removeprefix("sha256:"),
            }
            for artifact in delivery["artifacts"]
        ]
        for index, command in enumerate(supply_commands, start=1):
            argv = [value.replace("{manifest}", str(args.manifest.resolve())) for value in command]
            outcome = execute(CommandSpec.argv_command(argv, cwd=PATHS.project))
            if not outcome.ok:
                parser.error(f"供应链验证命令 {index} 失败: {outcome.stderr[-1000:]}")
            try:
                verification = json.loads(outcome.stdout)
            except json.JSONDecodeError:
                parser.error(f"供应链验证命令 {index} 必须输出 JSON")
            if not isinstance(verification, dict) or not isinstance(verification.get("artifacts"), list):
                parser.error(f"供应链验证命令 {index} 输出必须是含 artifacts 数组的对象")
            verified = verification["artifacts"]
            if (
                verification.get("manifest_digest") != delivery["manifest_digest"]
                or len(verified) != len(artifact_identities)
                or any(
                    not isinstance(item, dict)
                    or {key: item.get(key) for key in ("type", "ref", "digest")} != expected
                    or not all(item.get(field) is True for field in ("signature", "sbom", "build_once"))
                    for item, expected in zip(verified, artifact_identities, strict=True)
                )
            ):
                parser.error(f"供应链验证命令 {index} 未完整验证当前 Delivery 的全部制品")
            supply_evidence.append({"argv": argv, "returncode": outcome.returncode, "verification": verification})
        result = {
            "valid": True,
            "manifest": str(args.manifest.resolve()),
            "manifest_digest": delivery["manifest_digest"],
            "artifacts": artifact_identities,
            "supply_chain": supply_evidence,
        }
        target = StateStore(PATHS.state / "control/delivery-verifications").write_json(
            delivery["manifest_digest"].removeprefix("sha256:") + ".json", result
        )
        print(json.dumps({**result, "evidence": str(target)}, ensure_ascii=False))
    elif args.command == "release-compose":
        projects = load_managed_projects(managed_path)
        deliveries = [Path(value) for item in args.deliveries for value in str(item).split(",") if value]
        assert_registered_delivery_paths(deliveries, projects, PATHS.project)
        target = compose_release(args.release_id, deliveries, projects, PATHS.state, PATHS.project)
        print(json.dumps({"manifest": str(target)}, ensure_ascii=False))
    elif args.command in {"release-promote", "release-rollback"}:
        current_release = load_manifest(args.manifest)
        verification_commands = harness.get("control", {}).get("production_verification_commands", [])
        if args.command == "release-promote" and not verification_commands:
            parser.error("Production 提升必须配置 control.production_verification_commands")
        operation_store = StateStore(PATHS.state / "release-operations")
        operation_name = f"{current_release['release_id']}-{args.command}.json"
        operation = operation_store.read_json(operation_name, {})
        if args.command == "release-promote" and current_release.get("status") == "promoted":
            if operation.get("status") != "deployed" or operation.get("manifest_digest") != current_release.get(
                "manifest_digest"
            ):
                parser.error("Release 已提升且没有可恢复的同轮操作日志")
            promote_stable_release(PATHS.state, args.manifest, current_release)
            operation_store.write_json(operation_name, {**operation, "status": "complete"})
            print(
                json.dumps(
                    {"manifest": str(args.manifest), "release": current_release, "reconciled": True}, ensure_ascii=False
                )
            )
            return 0
        if args.command == "release-rollback" and current_release.get("status") == "rolled-back":
            if operation.get("status") != "deployed" or operation.get("manifest_digest") != current_release.get(
                "manifest_digest"
            ):
                parser.error("Release 已回滚且没有可恢复的同轮操作日志")
            complete_rollback(PATHS.state, args.manifest)
            operation_store.write_json(operation_name, {**operation, "status": "complete"})
            print(
                json.dumps(
                    {"manifest": str(args.manifest), "release": current_release, "reconciled": True}, ensure_ascii=False
                )
            )
            return 0
        deploy_manifest = (
            args.manifest if args.command == "release-promote" else resolve_rollback_release(PATHS.state, args.manifest)
        )
        deployed_release = load_manifest(deploy_manifest)
        expected = "test-verified" if args.command == "release-promote" else "promoted"
        if deployed_release.get("status") != expected:
            parser.error(f"待部署 Release 必须为 {expected}")
        if operation.get("status") == "deployed" and operation.get("manifest_digest") == current_release.get(
            "manifest_digest"
        ):
            evidence = operation.get("evidence")
            target_status = "promoted" if args.command == "release-promote" else "rolled-back"
            result = transition_release(args.manifest, target_status, evidence=[evidence])
            if args.command == "release-promote":
                promote_stable_release(PATHS.state, args.manifest, result)
            else:
                complete_rollback(PATHS.state, args.manifest)
            operation_store.write_json(operation_name, {**operation, "status": "complete"})
            print(
                json.dumps({"manifest": str(args.manifest), "release": result, "reconciled": True}, ensure_ascii=False)
            )
            return 0
        operation_store.write_json(
            operation_name,
            {
                "operation": args.command,
                "manifest": str(args.manifest.resolve()),
                "manifest_digest": current_release["manifest_digest"],
                "status": "deploying",
            },
        )
        stable_before = StateStore(PATHS.state / "releases").read_json("stable.json", {})
        baseline_path_value = (stable_before.get("current") or {}).get("manifest")
        baseline_path = Path(baseline_path_value) if baseline_path_value else None
        try:
            output = deploy_release(
                deploy_manifest,
                "prod",
                execute=True,
                production_authorized=True,
                baseline_manifest=baseline_path,
            )
        except Exception as exc:
            rollback_evidence = None
            rollback_error = None
            if baseline_path and baseline_path.resolve() != deploy_manifest.resolve():
                try:
                    rollback = deploy_release(
                        baseline_path,
                        "prod",
                        execute=True,
                        production_authorized=True,
                        baseline_manifest=deploy_manifest,
                    )
                    rollback_evidence = rollback.get("evidence")
                except Exception as recovery_exc:
                    rollback_error = str(recovery_exc)
            operation_store.write_json(
                operation_name,
                {
                    **operation_store.read_json(operation_name),
                    "status": "deployment-failed",
                    "error": str(exc),
                    "rollback_evidence": rollback_evidence,
                    "rollback_error": rollback_error,
                },
            )
            raise RuntimeError(f"Production 部署失败: {exc}; 恢复结果: {rollback_error or rollback_evidence}") from exc
        evidence = output.get("evidence")
        if not evidence:
            parser.error("部署未返回证据")
        if args.command == "release-promote":
            for index, command in enumerate(verification_commands, start=1):
                result = execute(CommandSpec.argv_command(command, cwd=PATHS.project))
                if not result.ok:
                    stable = StateStore(PATHS.state / "releases").read_json("stable.json", {})
                    previous_manifest = (stable.get("current") or {}).get("manifest")
                    rollback_evidence = None
                    rollback_error = None
                    recovery_checks = []
                    if previous_manifest:
                        try:
                            rollback = deploy_release(
                                Path(previous_manifest),
                                "prod",
                                execute=True,
                                production_authorized=True,
                                baseline_manifest=deploy_manifest,
                            )
                            rollback_evidence = rollback.get("evidence")
                            for verify_command in verification_commands:
                                restored = execute(CommandSpec.argv_command(verify_command, cwd=PATHS.project))
                                recovery_checks.append(
                                    {
                                        "argv": verify_command,
                                        "returncode": restored.returncode,
                                        "stdout": restored.stdout[-2000:],
                                        "stderr": restored.stderr[-2000:],
                                    }
                                )
                                if not restored.ok:
                                    rollback_error = "上一稳定版本健康检查失败"
                                    break
                        except Exception as recovery_exc:
                            rollback_error = str(recovery_exc)
                    operation_store.write_json(
                        operation_name,
                        {
                            **operation_store.read_json(operation_name),
                            "status": (
                                "recovery-required"
                                if not previous_manifest
                                else "recovery-failed"
                                if rollback_error
                                else "recovered-after-verification-failure"
                            ),
                            "failed_command": index,
                            "evidence": evidence,
                            "verification": {
                                "argv": command,
                                "returncode": result.returncode,
                                "stdout": result.stdout[-2000:],
                                "stderr": result.stderr[-2000:],
                            },
                            "rollback_evidence": rollback_evidence,
                            "rollback_error": rollback_error,
                            "recovery_verification": recovery_checks,
                        },
                    )
                    if not previous_manifest:
                        parser.error(f"Production verification command {index} failed；不存在上一稳定版本，需人工恢复")
                    if rollback_error:
                        parser.error(f"Production verification command {index} failed；回退失败: {rollback_error}")
                    parser.error(f"Production verification command {index} failed；已回退上一稳定版本")
        operation_store.write_json(
            operation_name,
            {**operation_store.read_json(operation_name), "status": "deployed", "evidence": evidence},
        )
        target_status = "promoted" if args.command == "release-promote" else "rolled-back"
        result = transition_release(args.manifest, target_status, evidence=[evidence])
        if args.command == "release-promote":
            promote_stable_release(PATHS.state, args.manifest, result)
        else:
            complete_rollback(PATHS.state, args.manifest)
        operation_store.write_json(operation_name, {**operation_store.read_json(operation_name), "status": "complete"})
        print(json.dumps({"manifest": str(args.manifest), "release": result, "evidence": evidence}, ensure_ascii=False))
    elif args.command == "release-transition":
        result = transition_release(args.manifest, args.status, evidence=args.evidence)
        print(json.dumps({"release": result, "manifest": str(args.manifest)}, ensure_ascii=False))
    else:
        release = load_manifest(args.manifest)
        if errors := validate_release(release):
            parser.error("Release Manifest 非法: " + "; ".join(errors))
        if release.get("status") != "failed":
            parser.error("integration-finding 只接受 Test Integration 已失败的 Release")
        existing = PATHS.state / "findings" / f"finding-{release['release_id']}.json"
        existing_finding = load_manifest(existing) if existing.is_file() else {}
        if not isinstance(existing_finding, dict) or existing_finding.get("release_id") != release["release_id"]:
            parser.error("缺少同一 Release 的 Test Integration 失败 Finding 证据")
        print(json.dumps({"finding": str(existing), "immutable": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

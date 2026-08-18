"""Versioned cross-project manifests and immutable-evidence validation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.state_store import StateStore

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
ASSIGNMENT_DECISIONS = {"accepted", "accepted_with_changes", "deferred", "rejected"}
ASSIGNMENT_TYPES = {"product", "architecture", "dependency"}


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _without(value: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in fields}


def now() -> str:
    return datetime.now(UTC).isoformat()


def require_id(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        errors.append(f"{field}: 必须是稳定 ID（小写字母、数字、._-）")


def require_reference(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not REFERENCE_PATTERN.fullmatch(value):
        errors.append(f"{field}: 必须是稳定引用（字母、数字、._-）")


def validate_assignment(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Assignment Manifest: 必须是对象"]
    for field in ("assignment_id", "source_project_id", "target_project_id", "idempotency_key"):
        require_id(data.get(field), field, errors)
    require_reference(data.get("source_reference"), "source_reference", errors)
    if data.get("assignment_type") not in ASSIGNMENT_TYPES:
        errors.append(f"assignment_type: 必须是 {sorted(ASSIGNMENT_TYPES)}")
    for field in ("outcome", "acceptance", "priority"):
        if not data.get(field):
            errors.append(f"{field}: 必填")
    if not isinstance(data.get("acceptance"), list) or not all(
        isinstance(item, str) and item.strip() for item in data.get("acceptance", [])
    ):
        errors.append("acceptance: 必须是非空字符串数组")
    if data.get("schema_version") != 2:
        errors.append("schema_version: 必须为 2")
    return errors


def validate_assignment_response(
    data: dict[str, Any], *, assignment: dict[str, Any] | None = None, project_id: str = ""
) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Assignment Response: 必须是对象"]
    for field in ("assignment_id", "source_project_id", "project_id"):
        require_id(data.get(field), field, errors)
    require_reference(data.get("source_reference"), "source_reference", errors)
    if data.get("schema_version") != 2:
        errors.append("schema_version: 必须为 2")
    if data.get("assignment_type") not in ASSIGNMENT_TYPES:
        errors.append(f"assignment_type: 必须是 {sorted(ASSIGNMENT_TYPES)}")
    if data.get("decision") not in ASSIGNMENT_DECISIONS:
        errors.append(f"decision: 必须是 {sorted(ASSIGNMENT_DECISIONS)}")
    if not isinstance(data.get("reason"), str) or not data["reason"].strip():
        errors.append("reason: 必填")
    if str(data.get("decision", "")).startswith("accepted") and not data.get("local_story"):
        errors.append("local_story: 接受 Assignment 时必填")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(data.get("assignment_digest", ""))):
        errors.append("assignment_digest: 必须是 SHA-256")
    recorded = data.get("manifest_digest")
    if not recorded or recorded != digest(_without(data, "manifest_digest")):
        errors.append("manifest_digest: 与 Assignment Response 内容不一致")
    if project_id and data.get("project_id") != project_id:
        errors.append(f"project_id: 必须是当前工程 {project_id}")
    if assignment:
        for field in ("assignment_id", "assignment_type", "source_project_id", "source_reference"):
            if data.get(field) != assignment.get(field):
                errors.append(f"{field}: 与 Assignment 不一致")
        if data.get("assignment_digest") != assignment.get("manifest_digest"):
            errors.append("assignment_digest: 与 Assignment 不一致")
    return errors


def validate_delivery(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Delivery Manifest: 必须是对象"]
    for field in ("delivery_id", "assignment_id", "project_id"):
        require_id(data.get(field), field, errors)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(data.get("source_commit", ""))):
        errors.append("source_commit: 必须是完整 Git object ID（SHA-1 或 SHA-256）")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(data.get("assignment_digest", ""))):
        errors.append("assignment_digest: 必须是 SHA-256")
    if data.get("schema_version") != 1:
        errors.append("schema_version: 必须为 1")
    quality = data.get("quality")
    if not isinstance(quality, dict) or quality.get("status") != "passed":
        errors.append("quality.status: 必须为 passed")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts: 至少包含一个不可变制品")
    else:
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict):
                errors.append(f"artifacts[{index}]: 必须是对象")
                continue
            ref = str(item.get("ref", ""))
            artifact_type = item.get("type")
            if artifact_type not in {"oci-image", "helm-chart", "client-package", "dependency-package"}:
                errors.append(f"artifacts[{index}].type: 非法制品类型")
                continue
            if not ref or "latest" in ref.lower():
                errors.append(f"artifacts[{index}].ref: 禁止空引用或 latest")
            if item.get("type") in {"oci-image", "helm-chart"} and not item.get("digest"):
                errors.append(f"artifacts[{index}].digest: OCI/Chart 必填")
            elif item.get("type") in {"oci-image", "helm-chart"} and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(item.get("digest", ""))
            ):
                errors.append(f"artifacts[{index}].digest: 必须是 sha256 内容摘要")
            if item.get("type") in {"client-package", "dependency-package"} and not item.get("sha256"):
                errors.append(f"artifacts[{index}].sha256: package 必填")
            elif item.get("type") in {"client-package", "dependency-package"} and not re.fullmatch(
                r"(?:sha256:)?[0-9a-f]{64}", str(item.get("sha256", ""))
            ):
                errors.append(f"artifacts[{index}].sha256: 必须是 SHA-256")
            if item.get("type") == "dependency-package":
                for field in ("package", "version"):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        errors.append(f"artifacts[{index}].{field}: dependency-package 必填")
                package = str(item.get("package", ""))
                version = str(item.get("version", ""))
                normalized_package = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", package))
                package_sha = str(item.get("sha256", "")).removeprefix("sha256:")
                if (
                    package
                    and version
                    and not re.search(
                        rf"/{normalized_package}-{re.escape(version)}[^/]*\.(?:whl|tar\.gz)#sha256={package_sha}$",
                        ref,
                        re.IGNORECASE,
                    )
                ):
                    errors.append(
                        f"artifacts[{index}].ref: dependency-package 必须以 package/version 文件名和 #sha256 绑定"
                    )
            if item.get("type") == "client-package":
                client_digest = str(item.get("sha256", "")).removeprefix("sha256:")
                if not re.search(rf"/(?:sha256:)?{re.escape(client_digest)}(?:/|\.|$)", ref):
                    errors.append(f"artifacts[{index}].ref: client-package 必须使用内容寻址路径绑定 SHA-256")
            if item.get("type") == "helm-chart":
                require_id(item.get("deployment_id"), f"artifacts[{index}].deployment_id", errors)
                if not item.get("version"):
                    errors.append(f"artifacts[{index}].version: Helm Chart 必填")
                if item.get("digest") and not ref.endswith("@" + str(item["digest"])):
                    errors.append(f"artifacts[{index}].ref: Helm Chart 必须以 @digest 固定内容")
            if item.get("type") in {"oci-image", "helm-chart", "client-package", "dependency-package"}:
                artifact_digest = (
                    str(item.get("digest", ""))
                    if item.get("type") not in {"client-package", "dependency-package"}
                    else "sha256:" + str(item.get("sha256", "")).removeprefix("sha256:")
                )
                if item.get("type") in {"oci-image", "helm-chart"} and not ref.endswith("@" + artifact_digest):
                    errors.append(f"artifacts[{index}].ref: OCI 制品必须以 @digest 固定内容")
                evidence_prefixes = {"signature": "signature://", "sbom": "sbom://", "build_once_evidence": "build://"}
                for field, prefix in evidence_prefixes.items():
                    if not item.get(field):
                        errors.append(f"artifacts[{index}].{field}: 供应链证据必填")
                    elif not re.fullmatch(
                        rf"{re.escape(prefix)}[^?#]+/{re.escape(artifact_digest)}/sha256:[0-9a-f]{{64}}",
                        str(item[field]),
                    ):
                        errors.append(f"artifacts[{index}].{field}: 必须同时绑定制品 digest 与证据摘要")
                try:
                    built_at = datetime.fromisoformat(str(item.get("built_at", "")).replace("Z", "+00:00"))
                    if built_at.tzinfo is None:
                        raise ValueError
                except ValueError:
                    errors.append(f"artifacts[{index}].built_at: 必须是含时区的 ISO-8601 时间")
                rollback = str(item.get("rollback_version", ""))
                if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?|sha256:[0-9a-f]{64}", rollback):
                    errors.append(f"artifacts[{index}].rollback_version: 必须是不可变版本")
    for index, contract in enumerate(data.get("contracts", [])):
        if not isinstance(contract, dict):
            errors.append(f"contracts[{index}]: 必须是对象")
            continue
        if (
            not contract.get("name")
            or not contract.get("version")
            or contract.get("role") not in {"provider", "consumer"}
        ):
            errors.append(f"contracts[{index}]: name/version/role 必须有效")
    recorded = data.get("manifest_digest")
    if not recorded:
        errors.append("manifest_digest: 必填")
    elif recorded != digest(_without(data, "manifest_digest", "published_at")):
        errors.append("manifest_digest: 与当前 Delivery 内容不一致")
    return errors


def delivery_artifact_identities(data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "type": str(artifact.get("type", "")),
            "ref": str(artifact.get("ref", "")),
            "digest": str(
                artifact.get("digest") or "sha256:" + str(artifact.get("sha256", "")).removeprefix("sha256:")
            ),
        }
        for artifact in data.get("artifacts", [])
    ]


def validate_delivery_verification(delivery: dict[str, Any], receipt: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict) or receipt.get("status") != "passed":
        return ["供应链验证 receipt.status 必须为 passed"]
    if receipt.get("manifest_digest") != delivery.get("manifest_digest"):
        errors.append("供应链验证 receipt 未绑定当前 Delivery")
    if receipt.get("artifacts") != delivery_artifact_identities(delivery):
        errors.append("供应链验证 receipt 未覆盖当前全部 Artifact")
    if not isinstance(receipt.get("verifiers"), list) or not receipt["verifiers"]:
        errors.append("供应链验证 receipt 缺少 verifier 证据")
    elif any(
        not isinstance(item, dict)
        or item.get("returncode") != 0
        or not all(item.get(field) is True for field in ("signature", "sbom", "build_once"))
        for item in receipt["verifiers"]
    ):
        errors.append("供应链验证 receipt 未证明签名、SBOM 与 Build Once")
    recorded = receipt.get("receipt_digest")
    if not recorded or recorded != digest(_without(receipt, "receipt_digest")):
        errors.append("供应链验证 receipt 摘要无效")
    return errors


def validate_release(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Release Manifest: 必须是对象"]
    require_id(data.get("release_id"), "release_id", errors)
    if data.get("schema_version") != 1:
        errors.append("schema_version: 必须为 1")
    if (
        data.get("composed_by") != "mai-harness"
        or not data.get("created_at")
        or not isinstance(data.get("dependency_order"), list)
    ):
        errors.append("composition: 必须包含 Harness 组合来源、时间与依赖顺序")
    deliveries = data.get("deliveries")
    if not isinstance(deliveries, list) or not deliveries:
        errors.append("deliveries: 至少包含一个交付")
    if data.get("status") not in {"composed", "test-verifying", "test-verified", "failed", "promoted", "rolled-back"}:
        errors.append("status: 非法 Release 状态")
    recorded = data.get("manifest_digest")
    immutable = _without(data, "manifest_digest", "status", "updated_at", "evidence")
    if not recorded:
        errors.append("manifest_digest: 必填")
    elif recorded != digest(immutable):
        errors.append("manifest_digest: 与 Release 不可变组合不一致")
    deployment_ids: set[str] = set()
    for delivery_index, delivery in enumerate(deliveries or []):
        if not isinstance(delivery, dict):
            errors.append(f"deliveries[{delivery_index}]: 必须是对象")
            continue
        if not delivery.get("manifest_digest"):
            errors.append(f"deliveries[{delivery_index}].manifest_digest: 必填")
        artifacts = delivery.get("artifacts") if isinstance(delivery, dict) else None
        if not isinstance(artifacts, list) or not artifacts:
            errors.append(f"deliveries[{delivery_index}].artifacts: 至少一个不可变制品")
            continue
        for artifact_index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                errors.append(f"deliveries[{delivery_index}].artifacts[{artifact_index}]: 必须是对象")
                continue
            if artifact.get("type") in {"oci-image", "helm-chart"} and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(artifact.get("digest", ""))
            ):
                errors.append(f"deliveries[{delivery_index}].artifacts[{artifact_index}].digest: 非法")
            if artifact.get("type") == "helm-chart" and not artifact.get("version"):
                errors.append(f"deliveries[{delivery_index}].artifacts[{artifact_index}].version: 必填")
            if artifact.get("type") == "helm-chart":
                require_id(
                    artifact.get("deployment_id"),
                    f"deliveries[{delivery_index}].artifacts[{artifact_index}].deployment_id",
                    errors,
                )
                deployment_id = artifact.get("deployment_id")
                if deployment_id in deployment_ids:
                    errors.append(f"deployment_id: Release 内重复 {deployment_id}")
                deployment_ids.add(deployment_id)
    return errors


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    StateStore(path.parent).write_text(path.name, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

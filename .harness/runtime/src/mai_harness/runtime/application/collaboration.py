"""Managed-project planning intake and local delivery services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import resolve_project_relative
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.manifest import (
    ASSIGNMENT_DECISIONS,
    digest,
    load_manifest,
    now,
    validate_assignment,
    validate_assignment_response,
    validate_delivery,
    validate_delivery_verification,
    write_manifest,
)
from mai_harness.runtime.infrastructure.utils import load_yaml


def _assignment_integrity(assignment: dict[str, Any]) -> list[str]:
    errors = validate_assignment(assignment)
    recorded = assignment.get("manifest_digest")
    payload = {key: value for key, value in assignment.items() if key not in {"dispatched_at", "manifest_digest"}}
    if not recorded or recorded != digest(payload):
        errors.append("manifest_digest: 与 Assignment 内容不一致")
    return errors


def _idempotency_key_paths(inbox: Path, key: str) -> list[Path]:
    matches = []
    for path in sorted(inbox.glob("*.json")):
        try:
            assignment = load_manifest(path)
        except (OSError, ValueError):
            continue
        if assignment.get("idempotency_key") == key:
            matches.append(path)
    return matches


def validate_delivery_publication(project: Path, path: Path, delivery: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_name = f"{delivery.get('delivery_id', '')}.json"
    if path.name != expected_name:
        errors.append(f"Delivery 文件名必须是 {expected_name}")
    receipt_path = project / ".harness/state/deliveries/idempotency" / expected_name
    if not receipt_path.exists():
        errors.append("Delivery 发布 receipt 不存在")
        return errors
    receipt = load_manifest(receipt_path)
    if (
        receipt.get("delivery_id") != delivery.get("delivery_id")
        or receipt.get("manifest_digest") != delivery.get("manifest_digest")
        or Path(str(receipt.get("target", ""))).resolve() != path.resolve()
    ):
        errors.append("Delivery 发布 receipt 与 ID、摘要或规范路径不一致")
    return errors


class AssignmentService:
    def __init__(self, project: Path, project_id: str, inbox: str, responses: str, deliveries: str = "") -> None:
        self.project = project
        self.project_id = project_id
        self.inbox = resolve_project_relative(project, inbox, "management.assignment_inbox")
        self.responses = resolve_project_relative(project, responses, "management.assignment_responses")
        self.deliveries = (
            resolve_project_relative(project, deliveries, "management.deliveries_dir") if deliveries else None
        )

    def pending(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.inbox.glob("*.json")) if self.inbox.exists() else []:
            try:
                assignment = load_manifest(path)
            except (OSError, ValueError) as exc:
                items.append(
                    {
                        "assignment_id": path.stem,
                        "outcome": "",
                        "status": "invalid",
                        "valid": False,
                        "invalid": True,
                        "errors": [f"Assignment JSON 无法解析: {exc}"],
                        "invalid_documents": [{"path": str(path), "errors": [str(exc)]}],
                        "deliveries": [],
                        "path": str(path),
                    }
                )
                continue
            errors = _assignment_integrity(assignment)
            if path.name != f"{assignment.get('assignment_id', '')}.json":
                errors.append("Assignment 文件名必须与 assignment_id 一致")
            if assignment.get("target_project_id") != self.project_id:
                errors.append(f"target_project_id 不是当前工程 {self.project_id}")
            key = str(assignment.get("idempotency_key", ""))
            if key and len(_idempotency_key_paths(self.inbox, key)) != 1:
                errors.append(f"idempotency_key: inbox 中不唯一 {key}")
            details = self.status(assignment.get("assignment_id", path.stem)) if not errors else None
            items.append(
                {
                    "assignment_id": assignment.get("assignment_id", path.stem),
                    "outcome": assignment.get("outcome", ""),
                    "status": details["state"] if details else "invalid",
                    "valid": not errors and details["state"] != "invalid",
                    "invalid": bool(errors) or details["state"] == "invalid",
                    "errors": errors if errors else details["errors"],
                    "invalid_documents": (
                        details["invalid_documents"]
                        if details
                        else [{"path": str(path), "assignment": assignment, "errors": errors}]
                    ),
                    "deliveries": details["deliveries"] if details else [],
                    "path": str(path),
                }
            )
        return items

    def respond(
        self, assignment_id: str, decision: str, *, reason: str, local_story: str = "", local_sprint: str = ""
    ) -> Path:
        if decision not in ASSIGNMENT_DECISIONS:
            raise ValueError(f"非法 Assignment 决定: {decision}")
        source = self.inbox / f"{assignment_id}.json"
        if not source.exists():
            raise FileNotFoundError(f"Assignment 不存在于规划收件箱: {assignment_id}")
        try:
            assignment = load_manifest(source)
        except (OSError, ValueError) as exc:
            return {
                "assignment_id": assignment_id,
                "state": "invalid",
                "deliveries": [],
                "errors": [f"Assignment JSON 无法解析: {exc}"],
                "invalid_documents": [{"path": str(source), "errors": [str(exc)]}],
            }
        errors = _assignment_integrity(assignment)
        if source.name != f"{assignment.get('assignment_id', '')}.json":
            errors.append("Assignment 文件名必须与 assignment_id 一致")
        if assignment.get("target_project_id") != self.project_id:
            errors.append(f"target_project_id: 目标不是当前工程 {self.project_id}")
        if errors:
            raise ValueError("Assignment 校验失败:\n- " + "\n- ".join(errors))
        if decision.startswith("accepted") and not local_story:
            raise ValueError("接受 Assignment 必须映射 local_story")
        response_payload = {
            "schema_version": 2,
            "assignment_id": assignment_id,
            "assignment_type": assignment["assignment_type"],
            "source_project_id": assignment["source_project_id"],
            "source_reference": assignment["source_reference"],
            "project_id": self.project_id,
            "decision": decision,
            "reason": reason,
            "local_story": local_story,
            "local_sprint": local_sprint,
            "responded_at": now(),
            "assignment_digest": assignment.get("manifest_digest") or digest(assignment),
        }
        response = {**response_payload, "manifest_digest": digest(response_payload)}
        target = self.responses / f"{assignment_id}.json"
        write_manifest(target, response)
        return target

    def status(self, assignment_id: str, *, requester_project_id: str = "") -> dict[str, Any]:
        source = self.inbox / f"{assignment_id}.json"
        if not source.exists():
            raise FileNotFoundError(f"Assignment 不存在: {assignment_id}")
        assignment = load_manifest(source)
        errors = _assignment_integrity(assignment)
        if assignment.get("target_project_id") != self.project_id:
            errors.append(f"target_project_id: 必须是当前工程 {self.project_id}")
        key = str(assignment.get("idempotency_key", ""))
        if key and len(_idempotency_key_paths(self.inbox, key)) != 1:
            errors.append(f"idempotency_key: inbox 中不唯一 {key}")
        if requester_project_id and assignment.get("source_project_id") != requester_project_id:
            raise PermissionError(f"Assignment {assignment_id} 不属于请求工程 {requester_project_id}")
        response_path = self.responses / f"{assignment_id}.json"
        deliveries = []
        invalid_documents = []
        if self.deliveries and self.deliveries.exists():
            delivery_paths = sorted(self.deliveries.glob("*.json"))
            parsed_deliveries = []
            for path in delivery_paths:
                try:
                    parsed_deliveries.append((path, load_manifest(path)))
                except (OSError, ValueError) as exc:
                    invalid_documents.append({"path": str(path), "errors": [f"Delivery JSON 无法解析: {exc}"]})
            delivery_ids = [item.get("delivery_id") for _, item in parsed_deliveries]
            for path, item in parsed_deliveries:
                if item.get("assignment_id") == assignment_id:
                    delivery_errors = validate_delivery(item)
                    delivery_errors.extend(validate_delivery_publication(self.project, path, item))
                    if delivery_ids.count(item.get("delivery_id")) != 1:
                        delivery_errors.append(f"delivery_id: deliveries 目录中不唯一 {item.get('delivery_id')}")
                    if item.get("project_id") != self.project_id:
                        delivery_errors.append(f"project_id: 必须是当前工程 {self.project_id}")
                    if item.get("assignment_digest") != assignment.get("manifest_digest"):
                        delivery_errors.append("assignment_digest: 与 Assignment 不一致")
                    if assignment.get("assignment_type") == "dependency" and not any(
                        artifact.get("type") == "dependency-package" for artifact in item.get("artifacts", [])
                    ):
                        delivery_errors.append("artifacts: dependency Assignment 必须交付 dependency-package")
                    verification_path = (
                        self.project
                        / ".harness/state/delivery-verifications"
                        / f"{str(item.get('manifest_digest', '')).removeprefix('sha256:')}.json"
                    )
                    if verification_path.exists():
                        delivery_errors.extend(validate_delivery_verification(item, load_manifest(verification_path)))
                    else:
                        delivery_errors.append("供应链验证 receipt 不存在")
                    document = {"path": str(path), "delivery": item}
                    if delivery_errors:
                        invalid_documents.append({**document, "errors": delivery_errors})
                    else:
                        deliveries.append(document)
        response = None
        if response_path.exists():
            try:
                response = load_manifest(response_path)
            except (OSError, ValueError) as exc:
                invalid_documents.append(
                    {"path": str(response_path), "errors": [f"Assignment Response JSON 无法解析: {exc}"]}
                )
            else:
                response_errors = validate_assignment_response(
                    response, assignment=assignment, project_id=self.project_id
                )
                if response_errors:
                    invalid_documents.append(
                        {"path": str(response_path), "response": response, "errors": response_errors}
                    )
        accepted_response = response is not None and response.get("decision") in {"accepted", "accepted_with_changes"}
        if deliveries and not accepted_response:
            invalid_documents.append(
                {"path": str(response_path), "errors": ["Delivery 必须先有有效的 accepted Response"]}
            )
        if errors or invalid_documents:
            state = "invalid"
        elif deliveries:
            state = "delivered"
        elif response is not None:
            decision = response.get("decision")
            state = "rejected" if decision == "rejected" else "deferred" if decision == "deferred" else "planned"
        else:
            state = "pending"
        return {
            "assignment_id": assignment_id,
            "state": state,
            "deliveries": deliveries,
            "errors": errors,
            "invalid_documents": invalid_documents,
        }


def target_assignment_service(target_project: Path) -> AssignmentService:
    config_path = target_project / "config/harness.yml"
    config = load_yaml(config_path)
    project = config.get("project", {})
    management = config.get("management", {})
    required = ("assignment_inbox", "assignment_responses", "deliveries_dir")
    if not project.get("id") or any(not management.get(field) for field in required):
        raise ValueError(f"目标工程未配置 Assignment 输入端口: {config_path}")
    return AssignmentService(
        target_project,
        project["id"],
        management["assignment_inbox"],
        management["assignment_responses"],
        management["deliveries_dir"],
    )


def dispatch_assignment(source_project_id: str, target_project: Path, assignment: dict[str, Any]) -> Path:
    errors = validate_assignment(assignment)
    if assignment.get("source_project_id") != source_project_id:
        errors.append(f"source_project_id: 必须是当前工程 {source_project_id}")
    service = target_assignment_service(target_project.resolve())
    if assignment.get("target_project_id") != service.project_id:
        errors.append(f"target_project_id: 必须是目标工程 {service.project_id}")
    if errors:
        raise ValueError("Assignment 分发校验失败:\n- " + "\n- ".join(errors))
    assignment_digest = digest(assignment)
    target = service.inbox / f"{assignment['assignment_id']}.json"
    document = {**assignment, "dispatched_at": now(), "manifest_digest": assignment_digest}
    state = StateStore(service.project / ".harness/state/assignments")
    receipt_name = f"idempotency/{assignment['idempotency_key']}.json"
    with state.lock("dispatch"):
        receipt = state.read_json(receipt_name)
        if not receipt and service.inbox.exists():
            matches = _idempotency_key_paths(service.inbox, assignment["idempotency_key"])
            if len(matches) > 1:
                raise ValueError(f"idempotency_key 冲突: {assignment['idempotency_key']}")
            for existing_path in matches:
                existing_assignment = load_manifest(existing_path)
                existing_errors = _assignment_integrity(existing_assignment)
                if (
                    existing_errors
                    or existing_assignment.get("assignment_id") != assignment["assignment_id"]
                    or existing_assignment.get("manifest_digest") != assignment_digest
                ):
                    raise ValueError(f"idempotency_key 冲突: {assignment['idempotency_key']}")
                receipt = {
                    "assignment_id": assignment["assignment_id"],
                    "manifest_digest": assignment_digest,
                    "target": str(existing_path),
                }
                state.write_json(receipt_name, receipt)
        if receipt and (
            receipt.get("assignment_id") != assignment["assignment_id"]
            or receipt.get("manifest_digest") != assignment_digest
        ):
            raise ValueError(f"idempotency_key 冲突: {assignment['idempotency_key']}")
        if target.exists():
            existing = load_manifest(target)
            integrity_errors = _assignment_integrity(existing)
            if integrity_errors or existing.get("manifest_digest") != assignment_digest:
                raise ValueError(f"Assignment ID 已存在且内容不同或已被改写: {target}")
        else:
            write_manifest(target, document)
        if not receipt:
            state.write_json(
                receipt_name,
                {
                    "assignment_id": assignment["assignment_id"],
                    "manifest_digest": assignment_digest,
                    "target": str(target),
                },
            )
        return target


def publish_delivery(project: Path, deliveries_dir: str, delivery: dict[str, Any]) -> Path:
    document = {**delivery, "published_at": now(), "manifest_digest": digest(delivery)}
    errors = validate_delivery(document)
    if errors:
        raise ValueError("Delivery 校验失败:\n- " + "\n- ".join(errors))
    root = resolve_project_relative(project, deliveries_dir, "management.deliveries_dir")
    target = root / f"{delivery['delivery_id']}.json"
    state = StateStore(project / ".harness/state/deliveries")
    receipt_name = f"idempotency/{delivery['delivery_id']}.json"
    with state.lock(f"publish.{delivery['delivery_id']}"):
        receipt = state.read_json(receipt_name)
        if receipt and receipt.get("manifest_digest") != document["manifest_digest"]:
            raise ValueError(f"Delivery ID 幂等冲突: {delivery['delivery_id']}")
        if target.exists():
            existing = load_manifest(target)
            existing_errors = validate_delivery(existing)
            if existing_errors or existing.get("manifest_digest") != document["manifest_digest"]:
                raise ValueError(f"Delivery ID 已存在且内容不同或已被改写: {target}")
        else:
            write_manifest(target, document)
        if not receipt:
            state.write_json(
                receipt_name,
                {
                    "delivery_id": delivery["delivery_id"],
                    "manifest_digest": document["manifest_digest"],
                    "target": str(target),
                },
            )
        return target

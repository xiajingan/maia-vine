"""Managed-project planning intake and local delivery services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import resolve_project_relative
from mai_harness.runtime.infrastructure.manifest import (
    ASSIGNMENT_DECISIONS,
    digest,
    load_manifest,
    now,
    validate_assignment,
    validate_delivery,
    write_manifest,
)


class AssignmentService:
    def __init__(self, project: Path, project_id: str, inbox: str, responses: str) -> None:
        self.project = project
        self.project_id = project_id
        self.inbox = resolve_project_relative(project, inbox, "management.assignment_inbox")
        self.responses = resolve_project_relative(project, responses, "management.assignment_responses")

    def pending(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.inbox.glob("*.json")) if self.inbox.exists() else []:
            assignment = load_manifest(path)
            errors = validate_assignment(assignment)
            if assignment.get("target_project_id") != self.project_id:
                errors.append(f"target_project_id 不是当前工程 {self.project_id}")
            response = self.responses / path.name
            items.append(
                {
                    "assignment_id": assignment.get("assignment_id", path.stem),
                    "outcome": assignment.get("outcome", ""),
                    "status": "responded" if response.exists() else "pending-planning",
                    "valid": not errors,
                    "errors": errors,
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
        assignment = load_manifest(source)
        errors = validate_assignment(assignment)
        if assignment.get("target_project_id") != self.project_id:
            errors.append(f"target_project_id: 目标不是当前工程 {self.project_id}")
        if errors:
            raise ValueError("Assignment 校验失败:\n- " + "\n- ".join(errors))
        if decision.startswith("accepted") and not local_story:
            raise ValueError("接受 Assignment 必须映射 local_story")
        response = {
            "schema_version": 1,
            "assignment_id": assignment_id,
            "control_requirement_id": assignment["control_requirement_id"],
            "project_id": self.project_id,
            "decision": decision,
            "reason": reason,
            "local_story": local_story,
            "local_sprint": local_sprint,
            "responded_at": now(),
            "assignment_digest": assignment.get("manifest_digest") or digest(assignment),
        }
        target = self.responses / f"{assignment_id}.json"
        write_manifest(target, response)
        return target

    def status(self, assignment_id: str) -> dict[str, Any]:
        source = self.inbox / f"{assignment_id}.json"
        if not source.exists():
            raise FileNotFoundError(f"Assignment 不存在: {assignment_id}")
        response = self.responses / f"{assignment_id}.json"
        return {"assignment_id": assignment_id, "state": "responded" if response.exists() else "pending-planning"}


def publish_delivery(project: Path, deliveries_dir: str, delivery: dict[str, Any]) -> Path:
    document = {**delivery, "published_at": now(), "manifest_digest": digest(delivery)}
    errors = validate_delivery(document)
    if errors:
        raise ValueError("Delivery 校验失败:\n- " + "\n- ".join(errors))
    root = resolve_project_relative(project, deliveries_dir, "management.deliveries_dir")
    target = root / f"{delivery['delivery_id']}.json"
    write_manifest(target, document)
    return target

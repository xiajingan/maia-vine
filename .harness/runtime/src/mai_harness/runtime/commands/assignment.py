"""Unified cross-project Assignment input and target-owned status port."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.collaboration import (
    AssignmentService,
    dispatch_assignment,
    target_assignment_service,
)
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.manifest import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("manifest", type=Path)
    dispatch.add_argument("--target-project", type=Path, required=True)
    sub.add_parser("pending")
    respond = sub.add_parser("respond")
    respond.add_argument("assignment_id")
    respond.add_argument("decision", choices=("accepted", "accepted_with_changes", "deferred", "rejected"))
    respond.add_argument("--reason", required=True)
    respond.add_argument("--local-story", default="")
    respond.add_argument("--local-sprint", default="")
    status = sub.add_parser("status")
    status.add_argument("assignment_id")
    status.add_argument("--target-project", type=Path)
    args = parser.parse_args()
    config = load_harness_config()
    if args.command == "dispatch":
        print(
            dispatch_assignment(
                config["project"]["id"],
                args.target_project,
                load_manifest(args.manifest),
            )
        )
        return 0
    if args.command == "status" and args.target_project:
        print(
            json.dumps(
                target_assignment_service(args.target_project.resolve()).status(
                    args.assignment_id, requester_project_id=config["project"]["id"]
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if config["project"]["mode"] != "managed":
        parser.error("pending/respond/local status 仅允许具有 Assignment 收件箱的 managed 模式")
    service = AssignmentService(
        PATHS.project,
        config["project"]["id"],
        config["management"]["assignment_inbox"],
        config["management"]["assignment_responses"],
        config["management"]["deliveries_dir"],
    )
    if args.command == "pending":
        print(json.dumps(service.pending(), ensure_ascii=False, indent=2))
    elif args.command == "respond":
        print(
            service.respond(
                args.assignment_id,
                args.decision,
                reason=args.reason,
                local_story=args.local_story,
                local_sprint=args.local_sprint,
            )
        )
    else:
        print(json.dumps(service.status(args.assignment_id), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Managed active-planning Assignment pending/respond/status port."""

from __future__ import annotations

import argparse
import json

from mai_harness.runtime.application.collaboration import AssignmentService
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending")
    respond = sub.add_parser("respond")
    respond.add_argument("assignment_id")
    respond.add_argument("decision", choices=("accepted", "accepted_with_changes", "deferred", "rejected"))
    respond.add_argument("--reason", required=True)
    respond.add_argument("--local-story", default="")
    respond.add_argument("--local-sprint", default="")
    status = sub.add_parser("status")
    status.add_argument("assignment_id")
    args = parser.parse_args()
    config = load_harness_config()
    if config["project"]["mode"] != "managed":
        parser.error("assignment 命令仅允许 managed 模式")
    service = AssignmentService(
        PATHS.project,
        config["project"]["id"],
        config["management"]["assignment_inbox"],
        config["management"]["assignment_responses"],
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

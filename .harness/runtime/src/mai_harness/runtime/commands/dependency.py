"""Coordinate synchronous consumer-to-library dependency development."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.dependency_session import (
    complete_session,
    provider_delivery_guard,
    record_candidate,
    start_session,
    validate_session,
    verify_consumer,
)
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("session_id")
    start.add_argument("--capability", required=True)
    start.add_argument("--consumer-sprint", required=True, type=Path)
    start.add_argument("--consumer-task", required=True)
    start.add_argument("--provider-project", required=True)
    start.add_argument("--provider-sprint", required=True)
    status = sub.add_parser("status")
    status.add_argument("session_id")
    candidate = sub.add_parser("candidate")
    candidate.add_argument("session_id")
    candidate.add_argument("--artifact", "--wheel", dest="artifact", required=True, type=Path)
    candidate.add_argument("--version", required=True)
    verify = sub.add_parser("verify-consumer")
    verify.add_argument("session_id")
    complete = sub.add_parser("complete")
    complete.add_argument("session_id")
    complete.add_argument("--delivery", required=True, type=Path)
    complete.add_argument("--lock", type=Path, default=Path("uv.lock"))
    guard = sub.add_parser("provider-delivery-guard")
    guard.add_argument("--sprint", required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.command == "start":
            result = start_session(
                root,
                session_id=args.session_id,
                capability_id=args.capability,
                consumer_sprint=args.consumer_sprint.resolve(),
                consumer_task_id=args.consumer_task,
                provider_project_id=args.provider_project,
                provider_sprint=args.provider_sprint,
            )
        elif args.command == "candidate":
            result = record_candidate(root, args.session_id, args.artifact, args.version)
        elif args.command == "verify-consumer":
            result = verify_consumer(root, args.session_id)
        elif args.command == "complete":
            result = complete_session(root, args.session_id, args.delivery, args.lock)
        elif args.command == "provider-delivery-guard":
            result = provider_delivery_guard(root, args.sprint)
        else:
            result = StateStore(root / ".harness/state/dependency-sessions").read_json(f"{args.session_id}.json")
            if not isinstance(result, dict):
                raise FileNotFoundError(f"Dependency session 不存在: {args.session_id}")
            errors = validate_session(result)
            if errors:
                raise ValueError("Dependency session 无效:\n- " + "\n- ".join(errors))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "session": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

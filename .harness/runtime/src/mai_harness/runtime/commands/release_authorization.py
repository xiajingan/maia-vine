#!/usr/bin/env python3
"""Create and verify a runtime-signed ask_user rollback authorization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.release_authorization import (
    create_rollback_challenge,
    issue_release_rollback_authorization,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    challenge = sub.add_parser("challenge")
    challenge.add_argument("authorization_id")
    challenge.add_argument("--target-ref", required=True)
    challenge.add_argument("--candidate-ref", required=True)
    challenge.add_argument("--interaction-id", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("authorization_id")
    issue.add_argument("--signoff", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    state_root = root / ".harness/state"
    try:
        if args.command == "challenge":
            path, record = create_rollback_challenge(
                state_root,
                args.authorization_id,
                args.target_ref,
                args.candidate_ref,
                args.interaction_id,
            )
        else:
            path, record = issue_release_rollback_authorization(root, state_root, args.authorization_id, args.signoff)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"path": str(path), "record": record}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

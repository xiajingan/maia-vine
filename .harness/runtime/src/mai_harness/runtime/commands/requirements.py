"""Synchronize user requirements and validate USER_STORIES as the product truth source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.requirements import (
    complete_sprint_stories,
    confirm_stories,
    record_feedback,
    sync_story,
)
from mai_harness.runtime.application.sprint_context import validate_sprint_activation
from mai_harness.runtime.domain.sprint_context import (
    sprint_header,
    sprint_planning_contract,
    sprint_uses_story_requirements,
    validate_user_stories,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--file", type=Path, default=Path("USER_STORIES.md"))
    intake = sub.add_parser("intake")
    intake.add_argument("--input", type=Path, required=True, dest="input_path")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("story_ids", nargs="+")
    confirm.add_argument("--by", required=True)
    feedback = sub.add_parser("feedback")
    feedback.add_argument("sprint")
    feedback.add_argument(
        "--classification",
        required=True,
        choices=("implementation-defect", "requirement-change", "new-story", "non-product"),
    )
    feedback.add_argument("--summary", required=True)
    feedback.add_argument("--by", required=True)
    feedback.add_argument("--story-input", type=Path)
    feedback.add_argument("--disposition", choices=("current-sprint", "backlog"), default="current-sprint")
    complete = sub.add_parser("complete")
    complete.add_argument("sprint")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.command == "validate":
            path = args.file if args.file.is_absolute() else root / args.file
            errors = validate_user_stories(path)
            print(json.dumps({"ok": not errors, "file": str(path), "errors": errors}, ensure_ascii=False, indent=2))
            return 1 if errors else 0
        if args.command == "intake":
            receipt = sync_story(
                root,
                args.input_path.resolve(),
                status="draft",
                action="intake",
            )
        elif args.command == "confirm":
            receipt = confirm_stories(root, args.story_ids, args.by)
        elif args.command == "feedback":
            receipt = record_feedback(
                root,
                args.sprint,
                args.classification,
                args.summary,
                args.by,
                input_path=args.story_input.resolve() if args.story_input else None,
                disposition=args.disposition,
            )
        else:
            plan = root / "docs/exec-plans/completed" / f"{args.sprint}.md"
            contract = sprint_planning_contract(plan) if plan.is_file() else {}
            version = contract.get("planning_contract_version")
            if (
                plan.is_file()
                and version is not None
                and sprint_uses_story_requirements(sprint_header(plan).get("sprint_type", ""), contract)
            ):
                activation_errors = validate_sprint_activation(
                    root,
                    plan,
                    require_completion_receipt=False,
                )
                if activation_errors:
                    raise ValueError("Story 完成前 Sprint 激活状态无效:\n- " + "\n- ".join(activation_errors))
            receipt = complete_sprint_stories(root, args.sprint)
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

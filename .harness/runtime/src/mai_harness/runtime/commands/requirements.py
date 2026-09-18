"""Synchronize user requirements and validate USER_STORIES as the product truth source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.human_approval import read_native_decision
from mai_harness.runtime.application.requirements import (
    complete_sprint_stories,
    confirm_stories,
    record_feedback,
    sync_story,
    validate_story_confirmations,
)
from mai_harness.runtime.application.requirements_selection import confirm_selection, prepare_selection
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
    select = sub.add_parser("select", help="提交本 Sprint 的具体 Story 内容及精确 AC 集合人工确认")
    select.add_argument("plan", type=Path)
    selection_source = select.add_mutually_exclusive_group()
    selection_source.add_argument("--from-host", action="store_true", help="读取已绑定 Codex 会话中的真实用户回复")
    selection_source.add_argument("--event", type=Path, help="宿主签署的人工决定；省略时生成待确认请求")
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
        elif args.command == "select":
            if args.event is None and not args.from_host:
                receipt = prepare_selection(root, args.plan.resolve())
            else:
                decision = (
                    read_native_decision(root, prepare_selection(root, args.plan.resolve()))
                    if args.from_host
                    else json.loads(args.event.read_text(encoding="utf-8"))
                )
                event = confirm_selection(root, args.plan.resolve(), decision)
                receipt = {"decision": event["decision"]}
                if event["decision"] == "approved":
                    story_ids = [
                        item["id"]
                        for item in sprint_planning_contract(args.plan)["source_stories"]
                        if validate_story_confirmations(root, [item])[1]
                    ]
                    if story_ids:
                        receipt["content_confirmation"] = confirm_stories(root, story_ids, event["actor"])
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

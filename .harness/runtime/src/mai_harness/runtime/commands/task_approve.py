"""Consume a host-recorded human decision and publish the exact reviewed design."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.design_completion import complete_design
from mai_harness.runtime.application.design_publication import publish_design, recover_publication
from mai_harness.runtime.application.human_approval import read_native_decision, record_decision
from mai_harness.runtime.application.task_evidence import current_attempt_state_path, load_current_attempt
from mai_harness.runtime.domain.design_policy import HUMAN_DESIGN_TASKS, file_digest
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type", nargs="?", choices=sorted(HUMAN_DESIGN_TASKS))
    parser.add_argument("sprint_path", nargs="?", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--digest", type=Path, help="输出受管文档的内容摘要，供登记 draft 索引")
    parser.add_argument("--complete-from", help="从已完成的实现/质量 Review 核对当前设计，发布 Done")
    decision_source = parser.add_mutually_exclusive_group()
    decision_source.add_argument("--from-host", action="store_true", help="读取已绑定 Codex 会话中的真实用户回复")
    decision_source.add_argument("--event", type=Path, help="宿主签署的真实用户交互 JSON；省略时恢复已批准的发布")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    if args.digest:
        path = args.digest.resolve()
        if not path.is_file() or not path.is_relative_to(root):
            parser.error("摘要只接受工程内实际文件")
        print(json.dumps({"path": path.relative_to(root).as_posix(), "sha256": file_digest(path)}))
        return 0
    if not all((args.task_type, args.sprint_path, args.task_id)):
        parser.error("批准设计需要 task_type、sprint_path 和 --task-id")
    plan = args.sprint_path.resolve()
    rules_path = HarnessPaths.detect(project=root).rules / "task-rules.yml"
    try:
        rules = load_yaml(rules_path)
        task = rules["tasks"][args.task_type]
        state_path = current_attempt_state_path(root, plan, args.task_id)
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        recover_publication(root, raw_state["run_id"])
        state = load_current_attempt(root, plan, rules_path, args.task_id, args.task_type, task)
        if state.get("review", {}).get("decision") != "pass" or not state.get("approval"):
            raise ValueError("先完成当前版本的 Agent Full Review，再提交具体产物人工审核")
        path = current_attempt_state_path(root, plan, args.task_id)
        if args.complete_from:
            if args.event or args.from_host:
                parser.error("设计完成验证不与新的人工决定混用")
            state = complete_design(root, plan, rules_path, rules, state, args.complete_from)
            print(
                json.dumps(
                    {"ok": True, "document_status": "done", "completion": state["completion"]}, ensure_ascii=False
                )
            )
            return 0
        if args.event or args.from_host:
            if args.from_host:
                decision = read_native_decision(root, state["approval"])
            else:
                event_path = args.event.resolve()
                if not event_path.is_relative_to(root) or not event_path.is_file():
                    raise ValueError("宿主事件文件必须位于当前工程")
                decision = json.loads(event_path.read_text(encoding="utf-8"))
            event = record_decision(root, state["approval"], decision)
            if event["decision"] == "rejected":
                state["human_status"] = "rejected"
                StateStore(root).write_json(path.relative_to(root), state)
                print(
                    json.dumps(
                        {"ok": True, "human_status": "rejected", "next": "修订原文档并重新 Review"}, ensure_ascii=False
                    )
                )
                return 0
        state = publish_design(root, path, state)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {"ok": True, "human_status": state["human_status"], "publication": state["publication"]}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

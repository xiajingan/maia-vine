"""Print the exact run paths and acceptance IDs for the active task attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import (
    ARCHITECTURE_BOUND_TASK_TYPES,
    PR_TASK_TYPES,
    acceptance_records,
    agent_invocation_id,
    bind_agent_invocation,
    finding_ledger,
    require_ready_attempt,
    required_agent_roles,
)
from mai_harness.runtime.domain.sprint_context import (
    sprint_header,
    sprint_planning_contract,
    sprint_uses_story_requirements,
)
from mai_harness.runtime.domain.task_protocol import execution_protocol, review_protocol
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--sprint", type=Path, required=True)
    parser.add_argument("--role", choices=("plan", "exec", "review"))
    parser.add_argument("--agent-invocation-id")
    parser.add_argument("--agent-runtime")
    args = parser.parse_args()
    binding_values = (args.role, args.agent_invocation_id, args.agent_runtime)
    if any(binding_values) and not all(binding_values):
        parser.error("--role、--agent-invocation-id、--agent-runtime 必须同时提供")
    root = Path.cwd().resolve()
    paths = HarnessPaths.detect(project=root)
    rules_path = paths.rules / "task-rules.yml"
    rules = load_yaml(rules_path)
    task = (rules.get("tasks") or {}).get(args.task_type)
    if not task:
        parser.error(f"未知任务类型: {args.task_type}")
    try:
        state = require_ready_attempt(root, args.sprint.resolve(), rules_path, args.task_id, args.task_type, task)
        facets = list((state.get("context") or {}).get("facets") or [])
        ledger = finding_ledger(root, args.sprint.resolve(), args.task_id)
        open_findings = [
            item
            for item in (ledger.get("findings") or {}).values()
            if isinstance(item, dict) and item.get("status") in {"open", "reopened"}
        ]
        advisories = []
        if len(facets) >= 4:
            advisories.append("任务同时覆盖至少 4 个质量/实现 facet；Plan 必须评估是否应拆分为独立验收单元")
        acceptance = acceptance_records(task, args.task_type, facets)
        if len(acceptance) >= 8:
            advisories.append("任务包含至少 8 条适用验收；Plan 必须检查是否存在多个可独立交付的不变式")
        agent_invocations = {
            role: {
                "instance_name": f"harness-{role}-{state['run_id']}",
                "invocation_id": agent_invocation_id(state["run_id"], role),
                "fresh_context_required": True,
                "reuse_forbidden": True,
            }
            for role in sorted(required_agent_roles(task))
        }
        upstream_inputs = (state.get("context") or {}).get("upstream_inputs", [])
        payload = {
            "run_id": state["run_id"],
            "attempt": state["attempt"],
            "run_dir": state["run_dir"],
            "plan": state["plan"],
            "review_report": state["review_report"],
            "facets": facets,
            "acceptance": acceptance,
            "finding_ledger": ".harness/state/tasks/findings/",
            "open_findings": open_findings,
            "planning_advisories": advisories,
            "execution_protocol": execution_protocol(task),
            "review_protocol": review_protocol(task),
            "agent_invocations": agent_invocations,
            "upstream_inputs": upstream_inputs,
        }
        contract = sprint_planning_contract(args.sprint)
        sprint_type = sprint_header(args.sprint).get("sprint_type", "")
        uses_stories = sprint_uses_story_requirements(sprint_type, contract)
        preferred_inputs = (rules.get("task_input_projections") or {}).get(args.task_type, [])
        requirements_are_integrity_only = bool(
            uses_stories
            and preferred_inputs
            and any(item.get("task_type") in preferred_inputs for item in upstream_inputs)
        )
        payload["requirements"] = {
            "mode": "stories" if uses_stories else contract.get("requirement_mode"),
            "access": "integrity-only" if requirements_are_integrity_only else "direct",
            "path": "USER_STORIES.md" if uses_stories and not requirements_are_integrity_only else None,
            "source_stories": (
                contract.get("source_stories") if uses_stories and not requirements_are_integrity_only else []
            ),
            "sha256": (state.get("context") or {}).get("requirements_sha256") if uses_stories else None,
            "change_protocol": "修改后执行 harness sprint amend --reason <reason>" if uses_stories else None,
            "reason": (
                "需求摘要仅用于漂移门禁；本任务的直接派生输入是 upstream_inputs 中的技术方案"
                if requirements_are_integrity_only
                else None
            ),
        }
        if args.task_type in ARCHITECTURE_BOUND_TASK_TYPES:
            payload["architecture"] = {
                "path": "ARCHITECTURE.md",
                "sha256": (state.get("context") or {}).get("architecture_sha256"),
                "change_protocol": "内容变化后重新运行当前任务 Preflight",
            }
        if entry_action := task.get("entry_action"):
            payload["entry_action"] = {
                "id": entry_action,
                "after": "plan" if execution_protocol(task) == "agent" else "pre-exec",
                "before": "exec" if execution_protocol(task) == "agent" else "completion",
                "command": [
                    "uv",
                    "run",
                    "--project",
                    ".harness/runtime",
                    "harness",
                    "task-action",
                    args.task_type,
                    "--task-id",
                    args.task_id,
                    "entry",
                    "--sprint",
                    str(args.sprint),
                ],
            }
        if args.task_type in PR_TASK_TYPES:
            payload["git_identity"] = {
                "policy": "registered-linear-head-v1",
                "base_sha": state["git_lineage"]["base_sha"],
                "registered_head_sha": state["git_lineage"]["head_sha"],
                "branch": state["git_lineage"]["branch"],
                "advance_command": [
                    "uv",
                    "run",
                    "--project",
                    ".harness/runtime",
                    "harness",
                    "task-commit",
                    args.task_type,
                    "--task-id",
                    args.task_id,
                    "--sprint",
                    str(args.sprint),
                    "--agent-invocation-id",
                    agent_invocations["exec"]["invocation_id"],
                ],
            }
        if args.role:
            bind_agent_invocation(
                root,
                args.sprint.resolve(),
                rules_path,
                args.task_id,
                args.task_type,
                task,
                role=args.role,
                invocation_id=args.agent_invocation_id,
                runtime=args.agent_runtime,
            )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

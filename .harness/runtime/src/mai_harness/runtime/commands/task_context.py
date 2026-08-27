"""Print the exact run paths and acceptance IDs for the active task attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import (
    PR_TASK_TYPES,
    acceptance_records,
    agent_invocation_id,
    bind_agent_invocation,
    finding_ledger,
    require_ready_attempt,
    required_agent_roles,
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
    task = (load_yaml(rules_path).get("tasks") or {}).get(args.task_type)
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

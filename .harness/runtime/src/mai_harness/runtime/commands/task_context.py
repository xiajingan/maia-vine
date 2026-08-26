"""Print the exact run paths and acceptance IDs for the active task attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import acceptance_records, finding_ledger, require_ready_attempt
from mai_harness.runtime.domain.task_protocol import execution_protocol, review_protocol
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--sprint", type=Path, required=True)
    args = parser.parse_args()
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
        }
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

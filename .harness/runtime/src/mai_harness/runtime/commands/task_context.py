"""Print the exact run paths and acceptance IDs for the active task attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import acceptance_records, require_ready_attempt
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
        payload = {
            "run_id": state["run_id"],
            "attempt": state["attempt"],
            "run_dir": state["run_dir"],
            "plan": state["plan"],
            "review_report": state["review_report"],
            "acceptance": acceptance_records(task, args.task_type),
            "execution_protocol": execution_protocol(task),
            "review_protocol": review_protocol(task),
        }
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

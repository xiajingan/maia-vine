"""Record the independent harness-review result for the active task attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import record_review
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("sprint_path", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--decision", choices=("pass", "fail", "incomplete"), required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument(
        "--architecture-candidate", type=Path, help="当前 attempt 内待人工批准的架构候选，禁止直接覆盖 ARCHITECTURE.md"
    )
    args = parser.parse_args()
    root = Path.cwd().resolve()
    paths = HarnessPaths.detect(project=root)
    rules_path = paths.rules / "task-rules.yml"
    task = (load_yaml(rules_path).get("tasks") or {}).get(args.task_type)
    if not task:
        parser.error(f"未知任务类型: {args.task_type}")
    try:
        evidence = record_review(
            root,
            args.sprint_path.resolve(),
            rules_path,
            args.task_id,
            args.task_type,
            task,
            args.report,
            args.decision,
            args.artifact,
            architecture_candidate=args.architecture_candidate,
        )
    except ValueError as exc:
        parser.error(str(exc))
    state = json.loads(evidence.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "ok": True,
                "evidence": str(evidence),
                "decision": args.decision,
                "human_status": state.get("human_status"),
                "approval": state.get("approval"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

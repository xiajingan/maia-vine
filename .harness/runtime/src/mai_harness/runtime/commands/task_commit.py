"""Bind a PR task's newly created commit to its active attempt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.task_evidence import advance_pr_head
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type", choices=("pr", "library-pr"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--sprint", type=Path, required=True)
    parser.add_argument("--agent-invocation-id", required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    paths = HarnessPaths.detect(project=root)
    rules_path = paths.rules / "task-rules.yml"
    task = (load_yaml(rules_path).get("tasks") or {}).get(args.task_type)
    if not task:
        parser.error(f"未知任务类型: {args.task_type}")
    try:
        state = advance_pr_head(
            root,
            args.sprint.resolve(),
            rules_path,
            args.task_id,
            args.task_type,
            task,
            invocation_id=args.agent_invocation_id,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": state["run_id"],
                "attempt": state["attempt"],
                "git_lineage": state["git_lineage"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

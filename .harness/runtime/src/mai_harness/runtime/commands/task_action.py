"""Run the deterministic action declared for one task phase."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from mai_harness.runtime.application.action_executor import execute_action
from mai_harness.runtime.application.task_evidence import record_phase, require_ready_attempt
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import load_yaml

PHASE_FIELDS = {
    "entry": ("entry_action",),
    "execute": ("execute", "action"),
}


def declared_action(task: dict, phase: str) -> tuple[str, frozenset[str]]:
    path = PHASE_FIELDS[phase]
    value = task
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"任务未声明 {phase} action")
    parameters = frozenset((task.get("execute") or {}).get("parameters", [])) if phase == "execute" else frozenset()
    return value, parameters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("phase", choices=tuple(PHASE_FIELDS))
    parser.add_argument("--sprint", type=Path)
    parser.add_argument("--value", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()
    if not args.sprint:
        parser.error("--sprint 必填，用于记录可审计的任务阶段证据")
    sprint_path = args.sprint.resolve()
    values = {"sprint": sprint_path.stem}
    for item in args.value:
        key, separator, value = item.partition("=")
        if not separator or not key:
            parser.error("--value 必须使用 KEY=VALUE")
        values[key] = value
    root = Path.cwd().resolve()
    paths = HarnessPaths.detect(project=root)
    task = (load_yaml(paths.rules / "task-rules.yml").get("tasks") or {}).get(args.task_type)
    if not task:
        parser.error(f"未知任务类型: {args.task_type}")
    try:
        action_id, declared_parameters = declared_action(task, args.phase)
        require_ready_attempt(root, sprint_path, paths.rules / "task-rules.yml", args.task_id, args.task_type, task)
        mode = load_harness_config(force=True).get("project", {}).get("mode")
        supplied_parameters = values.keys() - {"sprint"}
        if unknown := supplied_parameters - declared_parameters:
            raise ValueError(f"任务未声明 action 参数: {', '.join(sorted(unknown))}")
        if action_id == "control.integration.finding":
            _require_matching_failed_release(root, sprint_path, values.get("manifest", ""))
        outcome = execute_action(action_id, root=root, mode=mode, phase=args.phase, values=values)
    except ValueError as exc:
        parser.error(str(exc))
    evidence = record_phase(
        root,
        sprint_path,
        paths.rules / "task-rules.yml",
        args.task_id,
        args.task_type,
        task,
        args.phase,
        action_id,
        values,
        outcome.returncode,
        artifacts=_output_artifacts(root, outcome.stdout),
        diagnostic={
            "failure_kind": outcome.failure_kind or "exit",
            "duration_seconds": round(outcome.duration_seconds, 3),
            "stdout_bytes": len(outcome.stdout.encode()),
            "stdout_sha256": hashlib.sha256(outcome.stdout.encode()).hexdigest(),
            "stderr_bytes": len(outcome.stderr.encode()),
            "stderr_sha256": hashlib.sha256(outcome.stderr.encode()).hexdigest(),
        },
    )
    print(
        json.dumps(
            {"action": action_id, "ok": outcome.ok, "returncode": outcome.returncode, "evidence": str(evidence)},
            ensure_ascii=False,
        )
    )
    return outcome.returncode


def _require_matching_failed_release(root: Path, sprint_path: Path, manifest_value: str) -> None:
    """Bind a Finding Action to the exact failed Release artifact from this Sprint attempt."""
    from mai_harness.runtime.infrastructure.manifest import load_manifest

    manifest = Path(manifest_value)
    manifest = manifest if manifest.is_absolute() else root / manifest
    release = load_manifest(manifest)
    release_id = release.get("release_id") if isinstance(release, dict) else None
    expected = f".harness/state/findings/finding-{release_id}.json"
    states = root / ".harness/state/tasks"
    for path in states.glob(f"task-{sprint_path.stem}--*.json") if states.exists() else []:
        state = load_manifest(path)
        context = state.get("context") if isinstance(state, dict) else None
        if not isinstance(context, dict) or context.get("task_type") != "test-integration":
            continue
        phases = state.get("phases") or {}
        execute = phases.get("execute") or {} if isinstance(phases, dict) else {}
        artifacts = execute.get("artifacts") or [] if isinstance(execute, dict) else []
        if any(isinstance(item, dict) and item.get("path") == expected for item in artifacts):
            return
    raise ValueError("当前 Sprint 的 Test Integration 失败证据与目标 Release 不匹配")


def _output_artifacts(root: Path, stdout: str) -> list[Path]:
    """Extract existing project files from structured Action output."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    values: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "artifacts" and isinstance(child, list):
                    values.extend(item for item in child if isinstance(item, str))
                    continue
                if key in {"evidence", "target", "receipt", "manifest", "finding"} and isinstance(child, str):
                    values.append(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return [path for item in values if (path := Path(item) if Path(item).is_absolute() else root / item).is_file()]


if __name__ == "__main__":
    raise SystemExit(main())

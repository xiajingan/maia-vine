"""Execute registered Harness actions through the installed runtime dispatcher."""

from __future__ import annotations

from pathlib import Path

from mai_harness.runtime.domain.actions import bind_action, resolve_action
from mai_harness.runtime.infrastructure.core.command import CommandOutcome, CommandSpec, execute, harness_command


def action_argv(
    action_id: str,
    *,
    root: Path,
    mode: str,
    phase: str,
    values: dict[str, str] | None = None,
) -> tuple[str, ...]:
    action = resolve_action(action_id)
    if mode not in action.modes:
        raise ValueError(f"Action {action_id} 不允许用于 mode={mode}")
    if phase not in action.phases:
        raise ValueError(f"Action {action_id} 不允许用于 {phase} 阶段")
    return tuple(harness_command(*bind_action(action_id, values)))


def execute_action(
    action_id: str,
    *,
    root: Path,
    mode: str,
    phase: str,
    values: dict[str, str] | None = None,
) -> CommandOutcome:
    action = resolve_action(action_id)
    return execute(
        CommandSpec.argv_command(
            action_argv(action_id, root=root, mode=mode, phase=phase, values=values),
            cwd=root,
            timeout_seconds=action.timeout_seconds,
        )
    )

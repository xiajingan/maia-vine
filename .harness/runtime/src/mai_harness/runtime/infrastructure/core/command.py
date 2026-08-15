"""Structured and auditable external command execution."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


def harness_command(command: str, *args: str) -> list[str]:
    """Build the sole internal Harness CLI invocation without filesystem coupling."""
    return [sys.executable, "-m", "mai_harness.runtime.commands.harness_cli", command, *map(str, args)]


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...] | None = None
    shell_command: str | None = None
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    sensitive_env: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if bool(self.argv) == bool(self.shell_command):
            raise ValueError("CommandSpec 必须且只能声明 argv 或 shell_command")

    @classmethod
    def argv_command(cls, argv: Sequence[str], **kwargs) -> CommandSpec:
        return cls(argv=tuple(str(item) for item in argv), **kwargs)

    @classmethod
    def shell(cls, command: str, **kwargs) -> CommandSpec:
        if not command.strip():
            raise ValueError("shell command 不能为空")
        return cls(shell_command=command, **kwargs)

    def display(self) -> str:
        command = self.shell_command if self.shell_command is not None else shlex.join(self.argv or ())
        visible = {key: ("***" if key in self.sensitive_env else value) for key, value in self.env.items()}
        prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(visible.items()))
        return f"{prefix} {command}".strip()


@dataclass(frozen=True)
class CommandOutcome:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    display: str


def execute(spec: CommandSpec) -> CommandOutcome:
    command: Sequence[str] | str = spec.shell_command if spec.shell_command is not None else spec.argv or ()
    try:
        result = subprocess.run(
            command,
            cwd=spec.cwd,
            env={**os.environ, **spec.env},
            timeout=spec.timeout_seconds,
            shell=spec.shell_command is not None,
            check=False,
            text=True,
            capture_output=True,
        )
        return CommandOutcome(result.returncode == 0, result.stdout, result.stderr, result.returncode, spec.display())
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandOutcome(False, "", str(exc), getattr(exc, "returncode", 1) or 1, spec.display())

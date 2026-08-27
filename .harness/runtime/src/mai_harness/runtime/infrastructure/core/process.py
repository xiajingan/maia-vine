"""Managed long-running processes with deterministic group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ManagedProcess:
    process: subprocess.Popen

    @classmethod
    def start(
        cls, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None, quiet: bool = True
    ) -> ManagedProcess:
        stream = subprocess.DEVNULL if quiet else None
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env={**os.environ, **(env or {})},
            start_new_session=True,
            stdout=stream,
            stderr=stream,
        )
        return cls(process)

    @property
    def pid(self) -> int:
        return self.process.pid

    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.running():
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)

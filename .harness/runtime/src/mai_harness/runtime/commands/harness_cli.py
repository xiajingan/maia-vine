"""Stable installed command dispatcher for Harness runtime CLIs."""

from __future__ import annotations

import importlib
import sys

from mai_harness.runtime.commands.registry import resolve_command
from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: harness <command> [args...]", file=sys.stderr)
        return 2
    command = sys.argv.pop(1)
    if not command.replace("-", "").isalnum():
        print(f"invalid Harness command: {command}", file=sys.stderr)
        return 2
    try:
        registration = resolve_command(command)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    mode = load_harness_config()["project"]["mode"]
    if mode not in registration.modes:
        print(f"Harness command {command} is not allowed in mode={mode}", file=sys.stderr)
        return 2
    try:
        module = importlib.import_module(f"{__package__}.{registration.module}")
    except ModuleNotFoundError as exc:
        if exc.name == f"{__package__}.{registration.module}":
            print(f"invalid Harness registration: {command}", file=sys.stderr)
            return 2
        raise
    entry = getattr(module, "main", None)
    if not callable(entry):
        print(f"invalid Harness command: {command}", file=sys.stderr)
        return 2
    return int(entry() or 0)


if __name__ == "__main__":
    raise SystemExit(main())

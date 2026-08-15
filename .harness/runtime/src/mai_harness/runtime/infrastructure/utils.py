"""Shared filesystem, process, YAML, and Git helpers for Harness scripts."""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import yaml

from mai_harness.runtime.infrastructure.core.command import CommandOutcome, CommandSpec, execute


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def info(message: str) -> None:
    print(f"[harness] {message}")


def ok(message: str) -> None:
    print(f"✅ {message}")


def warn(message: str) -> None:
    print(f"⚠️  {message}", file=sys.stderr)


def err(message: str) -> None:
    print(f"❌ {message}", file=sys.stderr)


def fatal(message: str, code: int = 1) -> NoReturn:
    err(message)
    raise SystemExit(code)


def run(
    command: Sequence[str] | str,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    capture: bool = False,
    check: bool = True,
) -> CommandOutcome:
    del capture
    resolved_cwd = Path(cwd).resolve() if cwd is not None else None
    spec = (
        CommandSpec.shell(command, cwd=resolved_cwd, env=env or {}, timeout_seconds=timeout)
        if isinstance(command, str)
        else CommandSpec.argv_command(command, cwd=resolved_cwd, env=env or {}, timeout_seconds=timeout)
    )
    outcome = execute(spec)
    if check and not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout or f"command exited {outcome.returncode}")
    return outcome


def run_capture(command: Sequence[str] | str, **kwargs: Any) -> str:
    return run(command, capture=True, **kwargs).stdout.strip()


def try_run(command: Sequence[str] | str, **kwargs: Any) -> CommandResult:
    cwd_value = kwargs.pop("cwd", None)
    cwd = Path(cwd_value).resolve() if cwd_value is not None else None
    environment = kwargs.pop("env", None) or {}
    timeout = kwargs.pop("timeout", None)
    if kwargs:
        raise TypeError(f"try_run 不支持参数: {', '.join(kwargs)}")
    spec = (
        CommandSpec.shell(command, cwd=cwd, env=environment, timeout_seconds=timeout)
        if isinstance(command, str)
        else CommandSpec.argv_command(command, cwd=cwd, env=environment, timeout_seconds=timeout)
    )
    outcome = execute(spec)
    return CommandResult(outcome.ok, outcome.stdout, outcome.stderr, outcome.returncode)


def has_command(command: str) -> bool:
    return shutil.which(command) is not None


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def load_yaml(path: str | Path) -> Any:
    return yaml.safe_load(read_text(path)) or {}


def write_yaml(path: str | Path, value: Any) -> None:
    write_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))


def find_files(
    root: str | Path,
    predicate: Callable[[str], bool] | None = None,
    *,
    skip_dirs: Iterable[str] = (),
) -> list[str]:
    base = Path(root)
    skipped = set(skip_dirs)
    files: list[str] = []
    if not base.exists():
        return files
    for path in base.rglob("*"):
        if any(part in skipped for part in path.parts) or not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if predicate is None or predicate(relative):
            files.append((base / relative).as_posix())
    return sorted(files)


def git_root(cwd: str | Path | None = None) -> str:
    return run_capture(["git", "rev-parse", "--show-toplevel"], cwd=cwd)


def changed_files(base: str = "HEAD", cwd: str | Path | None = None) -> list[str]:
    result = try_run(["git", "diff", "--name-only", base], cwd=cwd)
    return [line for line in result.stdout.splitlines() if line] if result.ok else []


def current_branch(cwd: str | Path | None = None) -> str:
    result = try_run(["git", "branch", "--show-current"], cwd=cwd)
    return result.stdout.strip()


def parse_duration(value: str | int | float, default_seconds: int = 0) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(value))
    if not match:
        return default_seconds
    amount = int(match.group(1))
    return amount * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]

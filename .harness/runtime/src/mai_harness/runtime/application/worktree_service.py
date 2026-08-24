"""Shared creation and deterministic port allocation for linked worktrees."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute


def posix_cksum(value: str) -> int:
    data = value.encode()
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    length = len(data)
    while length:
        crc ^= (length & 0xFF) << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        length >>= 8
    return (~crc) & 0xFFFFFFFF


def allocated_ports(worktree_config: dict[str, Any], identifier: str) -> tuple[int, int]:
    offset = posix_cksum(identifier) % int(worktree_config["port_range"]) + 1
    return (
        int(worktree_config["ports"]["api_base"]) + offset,
        int(worktree_config["ports"]["web_base"]) + offset,
    )


def _run(argv: tuple[str, ...], cwd: Path, *, required: bool = True) -> None:
    outcome = execute(CommandSpec.argv_command(argv, cwd=cwd))
    if required and not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout or "worktree 命令失败")


def create_linked_worktree(
    root: Path,
    target: Path,
    branch: str,
    base_ref: str,
    identifier: str,
    worktree_config: dict[str, Any],
) -> None:
    if target.exists():
        raise FileExistsError(f"Worktree 已存在: {target}")
    _run(("git", "cat-file", "-e", f"{base_ref}^{{commit}}"), root)
    _run(("git", "worktree", "add", str(target), "-b", branch, base_ref), root)
    try:
        for adapter in worktree_config["dependency_adapters"]:
            if (target / adapter["marker"]).exists():
                _run(tuple(adapter["command"]), target, required=False)
                break
        api, web = allocated_ports(worktree_config, identifier)
        state = target / ".harness"
        state.mkdir(parents=True, exist_ok=True)
        (state / "ports").write_text(f"API_PORT={api}\nWEB_PORT={web}\n", encoding="utf-8")
    except Exception:
        _run(("git", "worktree", "remove", str(target), "--force"), root, required=False)
        _run(("git", "branch", "-D", branch), root, required=False)
        raise

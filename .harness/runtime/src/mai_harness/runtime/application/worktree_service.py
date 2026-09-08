"""Shared creation and deterministic port allocation for linked worktrees."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore


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


def _migrate_requirements_context(root: Path, target: Path, identifier: str) -> None:
    """Carry the current requirement truth and confirmations into a new isolated worktree."""
    stories = root / "USER_STORIES.md"
    if not stories.is_file():
        return
    target_stories = target / "USER_STORIES.md"
    StateStore(target).write_text("USER_STORIES.md", stories.read_text(encoding="utf-8"))
    source_confirmations = root / ".harness/state/requirements/confirmations"
    migrated: list[dict[str, str]] = []
    if source_confirmations.exists():
        destination = StateStore(target / ".harness/state/requirements/confirmations")
        for source in sorted(source_confirmations.glob("*.json")):
            if not source.is_file():
                continue
            content = source.read_text(encoding="utf-8")
            destination.write_text(source.name, content)
            migrated.append({"file": source.name, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    StateStore(target / ".harness/worktrees").write_json(
        "requirements-import.json",
        {
            "schema_version": 1,
            "sprint": identifier,
            "source_requirements_sha256": hashlib.sha256(stories.read_bytes()).hexdigest(),
            "target_requirements_sha256": hashlib.sha256(target_stories.read_bytes()).hexdigest(),
            "confirmations": migrated,
        },
    )


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
        _migrate_requirements_context(root, target, identifier)
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

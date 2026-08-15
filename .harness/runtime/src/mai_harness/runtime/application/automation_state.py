"""Durable Heartbeat run, lock, finding, and garbage-collection state."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.state_store import StateStore


def stable_hash(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def timestamp(value: str | int | float) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else float(value)


def run_key(*, job: str, source_sha: str, config_hash: str, scope: str = "repository") -> str:
    return stable_hash("\0".join((job, source_sha, config_hash, scope)))


def finding_key(*, rule: str, path: str = "", message: str = "") -> str:
    normalized = re.sub(r"\s+", " ", re.sub(r"\d+", "#", message)).strip()
    return stable_hash("\0".join((rule, path, normalized)))


def read_json(path: str | Path, fallback: Any = None) -> Any:
    target = Path(path)
    return json.loads(target.read_text(encoding="utf-8")) if target.exists() else fallback


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    StateStore(target.parent).write_json(target.name, value)


def acquire_lock(path: str | Path, owner: str, ttl_seconds: int, now_ms: int | None = None) -> bool:
    target = Path(path)
    now = (now_ms / 1000) if now_ms is not None else time.time()
    return StateStore(target.parent).acquire(target.name, owner, ttl_seconds, now=now)


def release_lock(path: str | Path, owner: str) -> None:
    target = Path(path)
    StateStore(target.parent).release(target.name, owner)


def state_paths(base: str | Path, job: str, identifier: str) -> dict[str, Path]:
    root = Path(base)
    return {
        "lock": root / "locks" / f"{job}.lock",
        "run": root / "runs" / f"{identifier}.json",
        "latest": root / "latest" / f"{job}.json",
        "findings": root / "findings",
    }


def upsert_finding(base: str | Path, value: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    timestamp = now or datetime.now(UTC).isoformat()
    fingerprint = value.get("fingerprint") or finding_key(
        rule=value["rule"], path=value.get("path", ""), message=value.get("message", "")
    )
    target = Path(base) / "findings" / f"{fingerprint}.json"
    previous = read_json(target, {})
    finding = {
        "fingerprint": fingerprint,
        "rule": value["rule"],
        "job": value["job"],
        "path": value.get("path", ""),
        "severity": value.get("severity", "medium"),
        "message": value.get("message", ""),
        "category": value.get("category", "code"),
        "status": "regressed" if previous.get("status") == "resolved" else previous.get("status", "new"),
        "first_seen": previous.get("first_seen", timestamp),
        "last_seen": timestamp,
        "occurrences": previous.get("occurrences", 0) + 1,
        "attempts": previous.get("attempts", 0),
        "active_pr": previous.get("active_pr"),
        "last_run_id": value.get("run_id") or previous.get("last_run_id"),
    }
    write_json(target, finding)
    return finding


def list_findings(base: str | Path) -> list[dict[str, Any]]:
    directory = Path(base) / "findings"
    return [read_json(path) for path in sorted(directory.glob("*.json"))] if directory.exists() else []


def classify_failure(exit_code: int = 1, output: str = "") -> str:
    text = output.lower()
    if exit_code == 126 or re.search(r"permission denied|operation not permitted|approval required", text):
        return "permission"
    if exit_code == 127 or re.search(
        r"command not found|enoent|docker.*not running|connection refused|timed out|network", text
    ):
        return "environment"
    return "code"


def gc_automation_state(
    base: str | Path, *, now_ms: int | None = None, resolved_ttl_days: int = 30, lock_ttl_seconds: int = 3600
) -> list[str]:
    now = (now_ms / 1000) if now_ms is not None else time.time()
    removed: list[str] = []
    for finding in list_findings(base):
        last_seen = datetime.fromisoformat(finding["last_seen"].replace("Z", "+00:00")).timestamp()
        if finding["status"] == "resolved" and now - last_seen > resolved_ttl_days * 86400:
            target = Path(base) / "findings" / f"{finding['fingerprint']}.json"
            target.unlink()
            removed.append(str(target))
    for target in (Path(base) / "locks").glob("*.lock") if (Path(base) / "locks").exists() else ():
        lock = read_json(target, {})
        acquired = timestamp(lock["acquired_at"])
        if now - acquired > lock_ttl_seconds:
            target.unlink()
            removed.append(str(target))
    return removed

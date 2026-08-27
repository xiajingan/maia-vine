"""Execute and validate source-bound backend performance evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.harness_config import resolve_command
from mai_harness.runtime.infrastructure.utils import try_run

BASE_FIELDS = {
    "schema_version",
    "artifact_type",
    "run_id",
    "source_commit",
    "worktree_digest",
    "command",
    "test_node",
    "started_at",
    "ended_at",
    "elapsed_seconds",
    "target_concurrency",
    "actual_concurrency",
    "p99_seconds",
    "categories",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}\Z")
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


@dataclass(frozen=True)
class PerformanceResult:
    ok: bool
    section: str
    evidence: dict[str, Any]


def _safe_relative_file(root: Path, value: object) -> Path:
    relative = Path(str(value))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
        raise ValueError("performance artifact path must be a safe relative JSON file")
    target = root.resolve() / relative
    if any(path.is_symlink() for path in (target, *target.parents) if path != root.resolve()):
        raise ValueError("performance artifact path contains a symlink")
    target.resolve(strict=False).relative_to(root.resolve())
    return target


def _head(root: Path) -> str:
    result = try_run(["git", "rev-parse", "HEAD"], cwd=root)
    value = result.stdout.strip()
    if not result.ok or not SHA_PATTERN.fullmatch(value):
        raise ValueError("cannot resolve source commit")
    return value


def worktree_identity(root: Path, paths: Sequence[str]) -> str:
    command = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths]
    result = try_run(command, cwd=root)
    if not result.ok:
        raise ValueError("cannot enumerate performance identity paths")
    digest = hashlib.sha256()
    for value in sorted(item for item in result.stdout.split("\0") if item):
        relative = Path(value)
        target = root / relative
        digest.update(relative.as_posix().encode() + b"\0")
        if not target.exists():
            digest.update(b"<missing>\0")
        elif target.is_symlink() or not target.is_file():
            raise ValueError("unsafe performance identity entry")
        else:
            digest.update(hashlib.sha256(target.read_bytes()).digest())
    return digest.hexdigest()


def _integer(document: Mapping[str, Any], field: str, minimum: int = 0) -> int:
    value = document.get(field)
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid performance integer: {field}")
    return value


def _number(document: Mapping[str, Any], field: str) -> float:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"invalid performance number: {field}")
    return float(value)


def _timestamp(document: Mapping[str, Any], field: str) -> datetime:
    value = document.get(field)
    if not isinstance(value, str):
        raise ValueError(f"invalid performance timestamp: {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid performance timestamp: {field}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"performance timestamp lacks timezone: {field}")
    return parsed


def _validate_schema(document: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    count_fields = list(config["count_fields"])
    zero_fields = list(config["zero_fields"])
    invariant_fields = set(count_fields) | set(zero_fields)
    if not count_fields or not zero_fields or invariant_fields & BASE_FIELDS:
        raise ValueError("backend performance invariant fields are invalid")
    if set(document) != BASE_FIELDS | invariant_fields:
        raise ValueError("backend performance evidence schema mismatch")
    return count_fields, zero_fields


def _validate_identity(
    document: Mapping[str, Any],
    config: Mapping[str, Any],
    command: Sequence[str],
    commit: str,
    identity: str,
) -> str:
    if _integer(document, "schema_version") != 1 or document.get("artifact_type") != config["artifact_type"]:
        raise ValueError("backend performance evidence type mismatch")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("backend performance run identity is invalid")
    if document.get("source_commit") != commit or document.get("worktree_digest") != identity:
        raise ValueError("backend performance source identity mismatch")
    if document.get("command") != list(command) or document.get("test_node") != config["test_node"]:
        raise ValueError("backend performance execution identity mismatch")
    return run_id


def _validate_measurements(document: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[float, int]:
    started, ended = _timestamp(document, "started_at"), _timestamp(document, "ended_at")
    elapsed = _number(document, "elapsed_seconds")
    if ended < started or elapsed < config["min_elapsed_seconds"]:
        raise ValueError("backend performance duration failed")
    if abs((ended - started).total_seconds() - elapsed) > config["duration_tolerance_seconds"]:
        raise ValueError("backend performance timestamps disagree")
    concurrency = config["target_concurrency"]
    if (
        _integer(document, "target_concurrency") != concurrency
        or _integer(document, "actual_concurrency") != concurrency
    ):
        raise ValueError("backend performance concurrency failed")
    if _number(document, "p99_seconds") >= config["max_p99_seconds"]:
        raise ValueError("backend performance p99 failed")
    return elapsed, concurrency


def _validate_invariants(
    document: Mapping[str, Any], config: Mapping[str, Any], count_fields: list[str], zero_fields: list[str]
) -> int:
    counts = [_integer(document, field, 1) for field in count_fields]
    if len(set(counts)) != 1:
        raise ValueError("backend performance counts disagree")
    categories = document.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(config["categories"]):
        raise ValueError("backend performance categories mismatch")
    if sum(_integer(categories, field, 1) for field in config["categories"]) != counts[0]:
        raise ValueError("backend performance category counts disagree")
    if any(_integer(document, field) != 0 for field in zero_fields):
        raise ValueError("backend performance zero invariant failed")
    return counts[0]


def validate_document(
    document: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    command: Sequence[str],
    commit: str,
    identity: str,
) -> dict[str, Any]:
    count_fields, zero_fields = _validate_schema(document, config)
    run_id = _validate_identity(document, config, command, commit, identity)
    elapsed, concurrency = _validate_measurements(document, config)
    total = _validate_invariants(document, config, count_fields, zero_fields)
    return {
        "artifact_type": document["artifact_type"],
        "run_id": run_id,
        "source_commit": commit,
        "worktree_digest": identity,
        "elapsed_seconds": elapsed,
        "concurrency": concurrency,
        "p99_seconds": _number(document, "p99_seconds"),
        "total": total,
    }


def collect_backend_performance(root: Path, harness: Mapping[str, Any]) -> PerformanceResult:
    config = harness["quality"].get("performance_evidence", {})
    command_name = config.get("command", "")
    if not command_name:
        return PerformanceResult(False, "- Backend performance producer: 未配置", {})
    command = resolve_command(harness["commands"].get(command_name, []))
    if not command:
        return PerformanceResult(False, "- Backend performance producer: 命令未定义", {})
    artifact: Path | None = None
    try:
        artifact = _safe_relative_file(root, config.get("artifact", ""))
        if artifact.is_symlink() or (artifact.exists() and not artifact.is_file()):
            raise ValueError("unsafe stale performance artifact")
        artifact.unlink(missing_ok=True)
        commit = _head(root)
        identity = worktree_identity(root, config["identity_paths"])
        run = try_run(command, cwd=root, timeout=float(config["timeout_seconds"]))
        if not run.ok or not artifact.is_file() or artifact.is_symlink():
            raise ValueError("backend performance producer failed")
        identity_after = worktree_identity(root, config["identity_paths"])
        if _head(root) != commit or identity_after != identity:
            raise ValueError("backend performance source changed during producer execution")
        document = json.loads(artifact.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("backend performance artifact must be an object")
        evidence = validate_document(document, config, command=command, commit=commit, identity=identity)
        identity_final = worktree_identity(root, config["identity_paths"])
        if _head(root) != commit or identity_after != identity_final:
            raise ValueError("backend performance source changed during validation")
        evidence["artifact"] = artifact.relative_to(root).as_posix()
        evidence["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        section = "\n".join(f"- {key}: {value}" for key, value in evidence.items())
        return PerformanceResult(True, section, evidence)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        if artifact and not artifact.is_dir():
            try:
                artifact.unlink(missing_ok=True)
            except OSError:
                pass
        return PerformanceResult(False, f"- Backend performance: FAIL ({error})", {})

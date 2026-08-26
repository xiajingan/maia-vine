"""Build one immutable Library artifact and record content-addressed evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from mai_harness.runtime.commands.quality_score import quality_report_basename
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import command_enabled, load_harness_config, resolve_command
from mai_harness.runtime.infrastructure.manifest import now
from mai_harness.runtime.infrastructure.package_identity import wheel_version
from mai_harness.runtime.infrastructure.technology_config import (
    load_technology_config,
    validate_technology_capabilities,
)


def artifact_snapshots(root: Path, pattern: str) -> dict[Path, tuple[str, int]]:
    snapshots = {}
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError(f"Library artifact glob 命中工程外文件: {path}")
        snapshots[resolved] = (hashlib.sha256(resolved.read_bytes()).hexdigest(), resolved.stat().st_mtime_ns)
    return snapshots


def restore_artifacts(root: Path, pattern: str, backup: Path, before: dict[Path, tuple[str, int]]) -> None:
    # Rollback must not resolve build-created symlinks: unlink the directory entry
    # itself so a malicious/broken artifact cannot escape the project or mask the
    # original build error before the failure receipt is persisted.
    for path in root.glob(pattern):
        if path.is_symlink() or path.is_file():
            path.unlink()
    for original in before:
        relative = original.relative_to(root)
        source = backup / relative
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, original)


def command_artifact(stdout: str, root: Path) -> tuple[Path, str, str] | None:
    """Read the language-neutral package_build JSON contract from the last output line."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(field), str) and payload[field].strip() for field in ("artifact", "package", "version")
    ):
        raise ValueError("package_build JSON 必须包含非空 artifact/package/version")
    declared = Path(payload["artifact"])
    artifact = declared.resolve() if declared.is_absolute() else (root / declared).resolve()
    return artifact, payload["package"].strip(), payload["version"].strip()


def quality_evidence(root: Path, sprint: str, source_commit: str) -> tuple[Path, str]:
    path = root / "docs/test-reports" / f"{quality_report_basename(sprint)}.json"
    if not path.is_file():
        raise ValueError(f"Build Once 缺少 Library quality JSON: {path.relative_to(root)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Library quality JSON 非法") from exc
    if data.get("passed") is not True:
        raise ValueError("Build Once 要求 Library quality PASS")
    if data.get("source_commit") != source_commit:
        raise ValueError("Build Once source commit 与 Library quality source commit 不一致")
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build(root: Path, sprint: str) -> dict:
    root = root.resolve()
    config = load_harness_config(force=True, path=root / "config/harness.yml")
    if config["project"]["type"] != "library":
        raise ValueError("library-package 仅允许 project.type=library")
    command = config.get("commands", {}).get("package_build", [])
    if not command:
        raise ValueError("commands.package_build 未配置")
    if not command_enabled(config, "package_build", root):
        raise ValueError("commands.package_build 条件未满足；非 Python Library 必须覆盖命令及 command_conditions")
    technology = load_technology_config(path=root / "config/technology.yml")
    if errors := validate_technology_capabilities(technology, config, root):
        raise ValueError("Library 技术栈能力未就绪:\n- " + "\n- ".join(errors))
    pattern = str(technology["components"]["library"]["artifact_glob"])
    commit = execute(CommandSpec.argv_command(("git", "rev-parse", "HEAD"), cwd=root))
    if not commit.ok or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit.stdout.strip()):
        raise ValueError("无法解析 Library source commit")
    status = execute(CommandSpec.argv_command(("git", "status", "--porcelain", "--untracked-files=all"), cwd=root))
    if not status.ok:
        raise ValueError("无法检查 Library worktree 状态")
    operational = (
        ".harness/state/",
        "dist/",
        "docs/test-reports/",
    )
    active_plan = f"docs/exec-plans/active/{sprint}.md"
    dirty = []
    for line in status.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path != active_plan and not path.startswith(operational):
            dirty.append(path)
    if dirty:
        raise ValueError("Build Once 要求源码已提交，worktree 仍有变更: " + ", ".join(dirty))
    quality_path, quality_sha256 = quality_evidence(root, sprint, commit.stdout.strip())
    state = StateStore(root / ".harness/state/library-packages")
    with state.lock(f"{sprint}.build", timeout_seconds=10):
        existing = state.read_json(f"{sprint}.json")
        if isinstance(existing, dict) and existing.get("source_commit") == commit.stdout.strip():
            existing_artifact = Path(str(existing.get("artifact") or existing.get("wheel", ""))).resolve()
            if (
                root in existing_artifact.parents
                and existing_artifact.is_file()
                and existing.get("sha256") == "sha256:" + hashlib.sha256(existing_artifact.read_bytes()).hexdigest()
            ):
                return {**existing, "evidence": str(state.root / f"{sprint}.json")}
        before = artifact_snapshots(root, pattern)
        with tempfile.TemporaryDirectory(prefix="mai-harness-library-backup-") as temporary:
            backup = Path(temporary)
            for original in before:
                target = backup / original.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
            try:
                outcome = execute(CommandSpec.argv_command(resolve_command(command), cwd=root, timeout_seconds=900))
                if not outcome.ok:
                    raise RuntimeError(outcome.stderr or outcome.stdout or "package_build 失败")
                after = artifact_snapshots(root, pattern)
                changed = [path for path, value in after.items() if before.get(path) != value]
                declared = command_artifact(outcome.stdout, root)
                if declared:
                    artifact, package, version = declared
                    if root not in artifact.parents or changed != [artifact]:
                        raise ValueError(
                            "package_build 必须在 artifact_glob 范围内唯一生成声明的 artifact，"
                            f"实际变更 {len(changed)} 个"
                        )
                else:
                    pyproject = root / "pyproject.toml"
                    if not pyproject.is_file():
                        raise ValueError("非 Python package_build 必须在最后一行输出 Artifact JSON")
                    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
                    package = str(project.get("name", "")).strip()
                    matching = [(path, wheel_version(path.name, package)) for path in changed]
                    matching = [(path, candidate_version) for path, candidate_version in matching if candidate_version]
                    if len(matching) != 1:
                        raise ValueError(
                            f"Python package_build 必须生成唯一且匹配 project.name 的新 wheel，实际 {len(matching)}"
                        )
                    artifact, version = matching[0]
                evidence = {
                    "schema_version": 1,
                    "sprint": sprint,
                    "package": package,
                    "version": version,
                    "artifact": str(artifact),
                    "sha256": "sha256:" + after[artifact][0],
                    "source_commit": commit.stdout.strip(),
                    "quality_evidence": str(quality_path),
                    "quality_evidence_sha256": quality_sha256,
                    "built_at": now(),
                }
                path = state.write_json(f"{sprint}.json", evidence)
                state.path(f"{sprint}.failure.json").unlink(missing_ok=True)
                return {**evidence, "evidence": str(path)}
            except BaseException as exc:
                restore_artifacts(root, pattern, backup, before)
                state.write_json(
                    f"{sprint}.failure.json",
                    {
                        "schema_version": 1,
                        "sprint": sprint,
                        "source_commit": commit.stdout.strip(),
                        "error": str(exc),
                        "failed_at": now(),
                    },
                )
                raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    args = parser.parse_args()
    try:
        result = build(Path.cwd().resolve(), args.sprint)
    except (FileNotFoundError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

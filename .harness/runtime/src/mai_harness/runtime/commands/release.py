#!/usr/bin/env python3
"""Release orchestration driven by Sprint evidence and immutable artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import harness_command
from mai_harness.runtime.infrastructure.utils import fatal, info, ok, run, run_capture, try_run, warn

ASSET_ROOT = Path("deploy/release")
VERSION_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(?:-[a-z0-9.]+)?$")
NOTES_TEMPLATE = """# Release vX.Y.Z

> 由 harness release 自动生成；release-prep 任务在此基础上补全章节。

- **发布时间**：<YYYY-MM-DD>
- **发布范围**：<纳入本次发布的 sprint id 列表>
- **责任人**：<release manager>
- **变更类型**：feat | fix | refactor | chore（多选）
"""
MIGRATION_TEMPLATE = 'version: 1\nrelease: vX.Y.Z\ncreated_at: ""\ncreated_by: ""\nitems: []\nsignature: ""\n'


def ensure_version(version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version or ""):
        fatal(f"版本号格式错误：{version}（期望 vX.Y.Z）")


def asset_dir(version: str) -> Path:
    return ASSET_ROOT / version


def resolve_test_ref(dry_run: bool) -> str | None:
    for ref in ("origin/test", "test"):
        if try_run(["git", "rev-parse", "--verify", "--quiet", ref]).ok:
            return ref
    return "origin/main" if dry_run else None


def validate_inputs(version: str, sprints: list[str], dry_run: bool) -> tuple[str, list[str], str | None]:
    ensure_version(version)
    try_run(["git", "fetch", "origin", "--no-tags"])
    branch, problems, test_ref = f"release/{version}", [], resolve_test_ref(dry_run)
    if try_run(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"]).ok:
        problems.append(f"origin/{branch} 已存在")
    if not test_ref:
        problems.append("未找到 test 分支引用")
    for sprint_id in sprints:
        series_match = re.match(r"^sprint-\d+", sprint_id)
        series = series_match.group(0) if series_match else sprint_id
        candidates = (
            Path(f"docs/acceptance-reports/{sprint_id}-boss-signoff.yml"),
            Path(f"docs/acceptance-reports/{series}-boss-signoff.yml"),
        )
        signoff = next((path for path in candidates if path.exists()), None)
        if signoff is None:
            problems.append(f"缺少 product-acceptance：{' / '.join(map(str, candidates))}")
            continue
        content = signoff.read_text(encoding="utf-8")
        if not re.search(r"^decision:\s*approved", content, re.MULTILINE):
            problems.append(f"sprint {sprint_id} 未 approved（{signoff}）")
            continue
        commit = re.search(r"^commit_sha:\s*([a-f0-9]{7,40})", content, re.MULTILINE)
        if not commit:
            problems.append(f"sprint {sprint_id} signoff 缺少 commit_sha")
        elif not test_ref or not try_run(["git", "merge-base", "--is-ancestor", commit.group(1), test_ref]).ok:
            problems.append(f"sprint {sprint_id} 的 commit {commit.group(1)} 未抵达 {test_ref or 'test'}")
    return branch, problems, test_ref


def scaffold(version: str, dry_run: bool) -> None:
    ensure_version(version)
    root = asset_dir(version)
    notes, manifest = root / "release-notes.md", root / "migrations/manifest.yml"
    if dry_run:
        info(f"scaffold dry-run: will create {notes} and {manifest}")
        return
    for subdir in (root, root / "migrations", root / "deploy", root / "observability"):
        subdir.mkdir(parents=True, exist_ok=True)
    if not notes.exists():
        template = Path("templates/release-notes.md")
        notes.write_text(
            (template.read_text(encoding="utf-8") if template.exists() else NOTES_TEMPLATE).replace("vX.Y.Z", version),
            encoding="utf-8",
        )
    if not manifest.exists():
        template = Path("templates/migration/manifest.yml.tpl")
        manifest.write_text(
            (template.read_text(encoding="utf-8") if template.exists() else MIGRATION_TEMPLATE).replace(
                "vX.Y.Z", version
            ),
            encoding="utf-8",
        )
    ok(f"{root} 骨架就绪")


def initialize(version: str, sprints: list[str], dry_run: bool) -> None:
    branch, problems, test_ref = validate_inputs(version, sprints, dry_run)
    if dry_run:
        info(f"release init dry-run: merge {test_ref or 'test'} into {branch}; sprints={','.join(sprints)}")
        if problems:
            warn("；".join(problems))
        return
    if problems:
        fatal("；".join(problems))
    run(["git", "switch", "-c", branch, "origin/main"])
    run(
        [
            "git",
            "merge",
            "--no-ff",
            str(test_ref),
            "-m",
            f"release({version}): include test HEAD; sprints={','.join(sprints)}",
        ]
    )
    scaffold(version, False)


def aggregate_quality(version: str, dry_run: bool) -> None:
    ensure_version(version)
    summaries: list[dict[str, object]] = []
    report_root = Path("docs/test-reports")
    for path in sorted(report_root.glob("*-quality.json")) if report_root.exists() else ():
        try:
            summaries.append(
                {"id": path.name.removesuffix("-quality.json"), "summary": json.loads(path.read_text(encoding="utf-8"))}
            )
        except json.JSONDecodeError:
            pass
    sprint_root = Path("sprints")
    for path in sorted(sprint_root.glob("*/quality-summary.json")) if sprint_root.exists() else ():
        try:
            summaries.append({"id": path.parent.name, "summary": json.loads(path.read_text(encoding="utf-8"))})
        except json.JSONDecodeError:
            pass
    if dry_run:
        info(f"aggregate-quality dry-run：将聚合 {len(summaries)} 个摘要")
        return
    if not summaries:
        fatal("未找到 sprint 质量摘要")
    target = asset_dir(version) / "quality-summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"release": version, "generated_at": datetime.now(UTC).isoformat(), "sprints": summaries},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    ok(f"质量聚合 → {target}")


def regression(version: str, dry_run: bool) -> None:
    ensure_version(version)
    if dry_run:
        info(f"regression dry-run: deploy test then quality L3 for {version}")
        return
    owner = f"release-{version}"
    run(harness_command("lock", "acquire", "test", "--owner", owner, "--ttl", "14400"))
    passed = False
    try:
        run(harness_command("deploy", "--env", "test", "--tag", f"release-{version}", "--driver", "compose"))
        run(harness_command("quality-score", "--level", "L3", "--release", version))
        passed = True
        ok(f"release/{version} 回归通过")
    finally:
        if not passed:
            try_run(harness_command("lock", "release", "test", "--owner", owner))


def create_pr(version: str, dry_run: bool) -> None:
    ensure_version(version)
    branch, notes = f"release/{version}", asset_dir(version) / "release-notes.md"
    if dry_run:
        info(f"release pr dry-run: branch={branch} base=main body={notes}")
        return
    run(["git", "push", "-u", "origin", branch])
    url = run_capture(
        harness_command(
            "pr-adapter",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"release: {version}",
            "--body-file",
            str(notes),
            "--labels",
            "release",
        )
    )
    ok(f"Release PR: {url}")


def approve(version: str, args: argparse.Namespace) -> None:
    ensure_version(version)
    target = asset_dir(version) / "approval.yml"
    decision, actor = ("rejected" if args.reject else "approved"), (args.by or os.environ.get("GITHUB_ACTOR") or "Boss")
    if args.dry_run:
        info(f"approve dry-run: target={target} decision={decision} by={actor}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = "\n".join(f"  {line}" for line in (args.summary or "").splitlines())
    target.write_text(
        f"release: {version}\ndecision: {decision}\nconfirmed_by: {actor}\nconfirmed_at: {datetime.now(UTC).isoformat()}\nsource: manual\nsummary: |\n{summary}\n",
        encoding="utf-8",
    )


def archive(version: str, dry_run: bool) -> None:
    ensure_version(version)
    if dry_run:
        info(f"archive dry-run: tag={version} delete=origin/release/{version}")
        return
    run(["git", "fetch", "origin", "main", "--tags"])
    if not try_run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{version}"]).ok:
        run(["git", "tag", "-a", version, "-m", f"release {version}", "origin/main"])
        run(["git", "push", "origin", version])
    if try_run(["git", "rev-parse", "--verify", "--quiet", f"origin/release/{version}"]).ok:
        run(["git", "push", "origin", "--delete", f"release/{version}"])
    try_run(harness_command("lock", "release", "test", "--owner", f"release-{version}"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "scaffold", "aggregate-quality", "regression", "pr", "approve", "archive")
    )
    parser.add_argument("version")
    parser.add_argument("--sprints", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--by")
    parser.add_argument("--summary", default="")
    parser.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.version, [item.strip() for item in args.sprints.split(",") if item.strip()], args.dry_run)
    elif args.command == "scaffold":
        scaffold(args.version, args.dry_run)
    elif args.command == "aggregate-quality":
        aggregate_quality(args.version, args.dry_run)
    elif args.command == "regression":
        regression(args.version, args.dry_run)
    elif args.command == "pr":
        create_pr(args.version, args.dry_run)
    elif args.command == "approve":
        approve(args.version, args)
    else:
        archive(args.version, args.dry_run)
    ok(f"release/{args.version} {args.command}{'(dry-run)' if args.dry_run else ''} 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

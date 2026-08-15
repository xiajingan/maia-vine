#!/usr/bin/env python3
"""Validate walkthrough artifacts and persist Boss approval decisions."""

import argparse
import re
from datetime import datetime
from pathlib import Path

from mai_harness.runtime.commands.task_rollback import rollback
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run, write_yaml


def paths_for(root: Path, sprint: str) -> dict[str, Path]:
    series = (re.match(r"^sprint-\d+", sprint) or re.match(r".*", sprint)).group(0)
    directory = root / "docs/acceptance-reports"

    def resolve(suffix: str) -> Path:
        for name in dict.fromkeys((sprint, series)):
            if (directory / f"{name}{suffix}").exists():
                return directory / f"{name}{suffix}"
        return directory / f"{sprint}{suffix}"

    return {
        "walkthrough": resolve("-walkthrough.md"),
        "acceptance": resolve("-acceptance.md"),
        "signoff": resolve("-boss-signoff.yml"),
    }


def lint_artifacts(files: dict[str, Path], require_signoff: bool, require_approved: bool) -> list[str]:
    errors = []
    requirements = {
        "walkthrough": ("## 环境信息", "## 设计对照", "## 功能走查路径", "## 版式整洁度检查", "预期结果"),
        "acceptance": (
            "## Boss 走查记录",
            "## 偏差清单",
            "## 结论",
            "### Critical",
            "### Major",
            "### Minor",
            "### Observation",
        ),
    }
    for kind, markers in requirements.items():
        if not files[kind].exists():
            errors.append(f"{kind} 不存在: {files[kind]}")
            continue
        text = files[kind].read_text(encoding="utf-8")
        errors += [f"{files[kind]} 缺少 {marker}" for marker in markers if marker not in text]
    if require_signoff or files["signoff"].exists():
        if not files["signoff"].exists():
            errors.append(f"审批记录不存在: {files['signoff']}")
        else:
            record = load_yaml(files["signoff"])
            for key in ("sprint", "decision", "confirmed_by", "confirmed_at", "source") + (
                ("commit_sha",) if require_approved else ()
            ):
                if not record.get(key):
                    errors.append(f"审批记录缺少字段: {key}")
            if require_approved and record.get("decision") != "approved":
                errors.append("审批记录未放行")
    return errors


def score_report(path: Path, rules_path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    rules = load_yaml(rules_path).get("pass_rules", {})
    failures = []

    def count(label: str) -> int:
        match = re.search(rf"###\s+{label}[^\n]*\n([\s\S]*?)(?=\n###\s+|\n##\s+|$)", text)
        return (
            len(re.findall(r"^\s*-\s+\S", match.group(1), re.M))
            if match and not re.search(r"^\s*(无|none|n/a)\s*$", match.group(1), re.I | re.M)
            else 0
        )

    for label, key, default in (("Critical", "critical_max", 0), ("Major", "major_max", 0), ("Minor", "minor_max", 5)):
        value = count(label)
        maximum = rules.get(key, default)
        if value > maximum:
            failures.append(f"{label} {value} > {maximum}")
    scores = [float(item) for item in re.findall(r"P-?[1-5]\D+(\d(?:\.\d)?)\s*/\s*5", text)]
    average = sum(scores) / len(scores) if scores else None
    if average is None or average < rules.get("product_stance_min", 4):
        failures.append("产品主张未评分或低于门槛")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("lint", "score", "approve", "reject"))
    parser.add_argument("sprint")
    parser.add_argument("--require-signoff", action="store_true")
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--by", default="Boss")
    parser.add_argument("--summary", default="")
    parser.add_argument("--commit-sha", default="")
    args = parser.parse_args()
    root = Path.cwd()
    files = paths_for(root, args.sprint)
    if args.action == "lint":
        errors = lint_artifacts(files, args.require_signoff, args.require_approved)
    elif args.action == "score":
        errors = (
            score_report(files["acceptance"], HarnessPaths.detect(project=root).rules / "walkthrough-checks.yml")
            if files["acceptance"].exists()
            else ["走查报告不存在"]
        )
    else:
        errors = lint_artifacts(files, False, False)
        if errors:
            for error in errors:
                print(f"❌ {error}")
            return 1
        sha = args.commit_sha or try_run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
        if args.action == "approve" and not sha:
            print("❌ approved 必须有 commit_sha")
            return 1
        target = root / "docs/acceptance-reports" / f"{args.sprint}-boss-signoff.yml"
        write_yaml(
            target,
            {
                "sprint": args.sprint,
                "decision": "approved" if args.action == "approve" else "rejected",
                "confirmed_by": args.by,
                "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "commit_sha": sha,
                "source": "ask_user",
                "summary": args.summary,
            },
        )
        if args.action == "reject":
            plan = root / "docs/exec-plans/active" / f"{args.sprint}.md"
            if plan.exists():
                updated, _ = rollback(
                    plan.read_text(encoding="utf-8"), "product-acceptance", "code", args.summary or "Boss 走查驳回"
                )
                plan.write_text(updated, encoding="utf-8")
        print(f"✅ 审批记录已写入: {target}")
        return 0
    for error in errors:
        print(f"❌ {error}")
    print("PASS" if not errors else "BLOCKED")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create hotfix branches/plans and trigger downstream back-merge."""

import argparse
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute, harness_command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("issue_id")
    init.add_argument("--severity", choices=("p0", "p1"), default="p1")
    init.add_argument("--from-tag")
    back = sub.add_parser("back-merge")
    back.add_argument("issue_id")
    args = parser.parse_args()
    root = Path.cwd()
    if args.command == "back-merge":
        return execute(
            CommandSpec.argv_command(
                [
                    *harness_command("back-merge"),
                    "--from",
                    "main",
                    "--to",
                    "test,develop",
                    "--reason",
                    f"hotfix-{args.issue_id}",
                ],
                cwd=root,
            )
        ).returncode
    tag = args.from_tag
    if not tag:
        result = execute(
            CommandSpec.argv_command(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"], cwd=root)
        )
        tag = result.stdout.strip() if result.ok else ""
    if not tag:
        print("❌ 未找到任何 v* tag")
        return 1
    for command in (["git", "fetch", "origin", "--tags"], ["git", "checkout", "-B", f"hotfix/{args.issue_id}", tag]):
        outcome = execute(CommandSpec.argv_command(command, cwd=root))
        if not outcome.ok:
            print(f"❌ {outcome.stderr or outcome.stdout}")
            return 1
    plan = root / "sprints" / f"hotfix-{args.issue_id}" / "PLAN.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    if not plan.exists():
        plan.write_text(
            f"# Hotfix 计划：{args.issue_id}\n\n- 严重度：{args.severity}\n- 基线 tag：{tag}\n- 分支：hotfix/{args.issue_id}\n\n## 现象\n\n<必填>\n\n## 根因\n\n<必填>\n\n## 修复方案\n\n<必填>\n\n## 验证\n\n- [ ] 回归测试\n- [ ] test 冒烟\n- [ ] L3 审批\n\n## 部署与回滚\n\n- 部署：`uv run --project .harness/runtime harness deploy --env prod`\n- 回滚：`uv run --project .harness/runtime harness deploy rollback --env prod`\n",
            encoding="utf-8",
        )
    print(f"✅ hotfix/{args.issue_id}（基于 {tag}）；计划：{plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

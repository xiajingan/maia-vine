"""Lint declared database and business ID boundaries (Golden Rules G-9 through G-11)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.domain.data_code_lint import check_fact
from mai_harness.runtime.domain.data_policy import validate_data_policy
from mai_harness.runtime.infrastructure.data_source_scan import source_facts, source_files
from mai_harness.runtime.infrastructure.technology_config import load_technology_config


def lint(root: Path, policy: dict | None) -> dict:
    if errors := validate_data_policy(policy):
        return {
            "status": "error",
            "files": 0,
            "findings": [
                {"file": "config/technology.yml", "line": 1, "rule": "G-10", "message": message} for message in errors
            ],
        }
    if policy is None:
        return {"status": "not-configured", "files": 0, "findings": []}
    try:
        root = root.resolve()
        files = source_files(root, policy)
        facts = source_facts(root, files)
        findings = [issue for fact in facts for issue in check_fact(fact, policy)]
        for issue in findings:
            issue["file"] = str(Path(issue["file"]).relative_to(root))
        findings = list({(v["file"], v["line"], v["rule"], v["message"]): v for v in findings}.values())
        return {
            "status": "fail" if findings else "pass" if files else "no-sources",
            "files": len(files),
            "findings": findings,
        }
    except (OSError, ValueError, SyntaxError) as exc:
        return {
            "status": "error",
            "files": 0,
            "findings": [{"file": "data-lint", "line": 1, "rule": "G-10", "message": str(exc)}],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()
    try:
        technology = load_technology_config(path=Path.cwd() / "config/technology.yml")
        report = lint(Path.cwd(), technology.get("data_policy"))
    except ValueError as exc:
        report = {
            "status": "error",
            "files": 0,
            "findings": [{"file": "config/technology.yml", "line": 1, "rule": "G-10", "message": str(exc)}],
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["findings"]:
            print(f"{item['file']}:{item['line']}: {item['rule']} {item['message']}")
        detail = {"not-configured": "未声明 data_policy，未执行检查", "no-sources": "策略有效，暂无适用源文件"}
        print(
            f"Harness data-lint: {detail.get(report['status'], report['status'])}; "
            f"{report['files']} files, {len(report['findings'])} findings"
        )
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

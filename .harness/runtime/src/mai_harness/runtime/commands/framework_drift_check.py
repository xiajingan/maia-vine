#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

WATCH: list[str] = []
PROJECT_OWN: list[str] = []


def check_one(
    relative: str, *, project_scripts_dir: Path, framework_scripts_dir: Path, project_own: list[str] | None = None
) -> dict[str, str]:
    if Path(relative).name in (project_own if project_own is not None else PROJECT_OWN):
        return {"status": "WHITELIST-SKIP", "file": relative, "reason": "PROJECT_OWN whitelist"}
    project, framework = project_scripts_dir / relative, framework_scripts_dir / relative
    if not project.exists() or not framework.exists():
        return {
            "status": "MISSING",
            "file": relative,
            "reason": f"missing: {project if not project.exists() else framework}",
        }

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    left, right = digest(project), digest(framework)
    return {
        "status": "IDENTICAL" if left == right else "DRIFT",
        "file": relative,
        "project_sha": left,
        "framework_sha": right,
    }


def run_drift_check(
    *,
    watch: list[str] | None = None,
    project_own: list[str] | None = None,
    framework_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root or Path.cwd())
    framework = Path(framework_path or os.environ.get("MAI_HARNESS_PATH", root.parent / "mai-harness"))
    if not framework.is_dir():
        return {
            "framework_exists": False,
            "framework_path": str(framework),
            "results": [],
            "summary": {"identical": 0, "drift": 0, "missing": 0, "whitelist": 0},
        }
    results = [
        check_one(
            item,
            project_scripts_dir=root / "scripts",
            framework_scripts_dir=framework / "scripts",
            project_own=project_own,
        )
        for item in (watch if watch is not None else WATCH)
    ]
    summary = {
        "identical": sum(x["status"] == "IDENTICAL" for x in results),
        "drift": sum(x["status"] == "DRIFT" for x in results),
        "missing": sum(x["status"] == "MISSING" for x in results),
        "whitelist": sum(x["status"] == "WHITELIST-SKIP" for x in results),
    }
    return {"framework_exists": True, "framework_path": str(framework), "results": results, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework-path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.dry_run or not WATCH:
        print(
            json.dumps({"watch": WATCH, "project_own": PROJECT_OWN}, indent=2)
            if args.json
            else "WATCH=\n  (空)\nPROJECT_OWN=\n  (空)"
        )
        return 0
    report = run_drift_check(framework_path=args.framework_path)
    print(
        json.dumps(report, indent=2)
        if args.json
        else "\n".join(f"{item['status']:15} {item['file']}" for item in report["results"])
    )
    return 1 if report["summary"]["drift"] + report["summary"]["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

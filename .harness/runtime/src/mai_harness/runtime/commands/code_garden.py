#!/usr/bin/env python3
import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import ok, warn

SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"}


def scan(source: Path, *, e2e_root: Path | None = None) -> dict[str, object]:
    files = [
        path
        for path in source.rglob("*")
        if path.suffix in SOURCE_EXTENSIONS
        and not re.search(r"\.(test|spec)\.", path.name)
        and not {"node_modules", ".next", "dist", "build", "__tests__"}.intersection(path.parts)
    ]
    todos = sum(
        len(re.findall(r"TODO|FIXME|HACK|XXX", path.read_text(encoding="utf-8", errors="ignore"))) for path in files
    )
    resolved_e2e_root = e2e_root or PATHS.e2e
    specs = list(resolved_e2e_root.rglob("*.spec.*")) if resolved_e2e_root.exists() else []
    score = 100 - min((todos // 5) * 5, 25) - (0 if specs else 20)
    grade = next(
        letter for threshold, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F")) if score >= threshold
    )
    return {"files": len(files), "todos": todos, "e2e": len(specs), "score": score, "grade": grade}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="src")
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    source = Path(args.src)
    if not source.exists():
        raise SystemExit(f"源码目录不存在: {source}")
    result = scan(source)
    print(
        f"代码质量等级: {result['grade']} ({result['score']}/100)\n文件总数: {result['files']}\nTODO/FIXME: {result['todos']}\nE2E: {result['e2e']}"
    )
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# Code Garden Report\n\n| 指标 | 值 |\n|---|---|\n| 质量等级 | {result['grade']} ({result['score']}/100) |\n| 文件总数 | {result['files']} |\n| TODO/FIXME | {result['todos']} |\n| E2E | {result['e2e']} |\n\n生成时间: {datetime.now(UTC).isoformat()}\n",
            encoding="utf-8",
        )
    ok("code-garden 完成") if result["grade"] != "F" else warn("质量等级 F")
    return 1 if args.ci and result["grade"] == "F" else 0


if __name__ == "__main__":
    raise SystemExit(main())

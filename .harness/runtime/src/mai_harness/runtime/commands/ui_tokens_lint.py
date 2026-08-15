#!/usr/bin/env python3
"""Scan UI source for hard-coded values that bypass design tokens."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

PALETTES = "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
SIZE_PROPERTIES = "margin|padding|gap|font-size|width|height|min-width|min-height|max-width|max-height|top|right|bottom|left|line-height|letter-spacing|border-radius"
PATTERNS = {
    "no-hex-color": re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b"),
    "no-rgb-color": re.compile(r"\brgba?\s*\("),
    "no-hsl-color": re.compile(r"\bhsla?\s*\("),
    "no-raw-tw-palette": re.compile(
        rf"\b(?:bg|text|border|ring|from|to|via|fill|stroke|divide|placeholder|caret|accent|outline|shadow|decoration)-(?:{PALETTES})-\d{{2,3}}\b"
    ),
    "no-magic-px": re.compile(rf"\b(?:{SIZE_PROPERTIES})(?:-[a-z]+)?\s*:\s*([0-9]+)px\b", re.I),
}
EXTENSIONS = {".vue", ".css", ".scss", ".sass", ".less", ".ts", ".tsx", ".js", ".jsx", ".html"}
SKIP_DIRS = {"node_modules", "dist", "build", ".git", ".next", ".nuxt", "coverage", "templates"}
SKIP_FILES = {"tokens.css", "prototype-base.html"}


@dataclass
class Violation:
    file: str
    line: int
    column: int
    rule: str
    snippet: str


def scan_file(file: Path, display_root: Path) -> list[Violation]:
    if file.name in SKIP_FILES:
        return []
    content = file.read_text(encoding="utf-8", errors="ignore")
    if "token-lint-disable" in content[:400]:
        return []
    violations: list[Violation] = []
    disable_next = False
    for number, raw in enumerate(content.splitlines(), 1):
        if disable_next:
            disable_next = False
            continue
        if "token-lint-disable-next-line" in raw:
            disable_next = True
            continue
        if "token-lint-disable-line" in raw:
            continue
        code = re.sub(r"//.*$|/\*.*?\*/|<!--.*?-->", "", raw)
        for rule, pattern in PATTERNS.items():
            for match in pattern.finditer(code):
                if rule == "no-hex-color" and "data:image/" in raw[max(0, match.start() - 60) : match.start()]:
                    continue
                if rule == "no-rgb-color" and "var(" in code[match.start() : match.start() + 60]:
                    continue
                if rule == "no-magic-px" and int(match.group(1)) in {0, 1, 2}:
                    continue
                violations.append(
                    Violation(
                        file.relative_to(display_root).as_posix(), number, match.start() + 1, rule, match.group(0)
                    )
                )
    return violations


def scan(directories: list[Path], display_root: Path) -> tuple[int, list[Violation]]:
    files = sorted(
        {
            file
            for directory in directories
            if directory.exists()
            for file in directory.rglob("*")
            if file.is_file() and file.suffix in EXTENSIONS and not SKIP_DIRS.intersection(file.parts)
        }
    )
    return len(files), [item for file in files for item in scan_file(file, display_root)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="src,web/src,packages")
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    directories = [(root / item.strip()).resolve() for item in args.dir.split(",") if item.strip()]
    count, violations = scan(directories, root)
    for item in violations:
        print(f"{item.file}:{item.line}:{item.column} [{item.rule}] {item.snippet}")
    print(f"UI Token Lint — 扫描 {count} 个文件，发现 {len(violations)} 项违规")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            "# UI Token Lint Report",
            "",
            f"Files scanned: {count}",
            f"Violations: {len(violations)}",
            "",
            *[f"- `{v.file}:{v.line}:{v.column}` **{v.rule}** `{v.snippet}`" for v in violations],
            "",
        ]
        args.report.write_text("\n".join(rows), encoding="utf-8")
    return 1 if args.ci and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())

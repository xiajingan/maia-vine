#!/usr/bin/env python3
"""Validate the Harness documentation knowledge base."""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path

from mai_harness.runtime.infrastructure.utils import has_command, load_yaml, try_run

INDEX_DIRS = (
    "product-specs",
    "design-docs",
    "tech-docs",
    "review-reports",
    "test-reports",
    "acceptance-reports",
    "observability-reports",
    "bugs",
)
REQUIRED_SPECS = (
    "SPRINT.md",
    "PRODUCT_SENSE.md",
    "DESIGN.md",
    "CODING_BACKEND.md",
    "CODING_FRONTEND.md",
    "TECH_BACKEND.md",
    "TECH_FRONTEND.md",
    "CODE_REVIEW.md",
    "QUALITY_SCORE.md",
    "PRODUCT_ACCEPTANCE.md",
    "RELEASE.md",
    "OBSERVABILITY.md",
    "GOLDEN_RULES.md",
)
MAX_DOC_LINES = 500


@dataclass
class Finding:
    level: str
    message: str


def markdown_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def internal_links(text: str) -> list[str]:
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", text)
    return [link for link in links if not re.match(r"^(https?://|mailto:|#)", link)]


def link_exists(source: Path, link: str, project_root: Path) -> bool:
    target = link.split("#", 1)[0]
    if not target:
        return True
    if (source.parent / target).exists():
        return True
    match = re.fullmatch(r"\.\./(ARCHITECTURE|PROJECT_RULES|USER_STORIES)\.md", target)
    return bool(match and (project_root / "templates" / f"{match.group(1)}.md").exists())


def lint_docs(
    docs_dir: Path,
    *,
    project_root: Path | None = None,
    max_age: int = 30,
    check_freshness: bool = True,
) -> list[Finding]:
    root = (project_root or Path.cwd()).resolve()
    docs = docs_dir.resolve()
    findings: list[Finding] = []

    def add(level: str, message: str) -> None:
        findings.append(Finding(level, message))

    for file in markdown_files(docs):
        text = file.read_text(encoding="utf-8")
        for link in internal_links(text):
            if not link_exists(file, link, root):
                add("error", f"断链: {file} → {link}")

    for name in INDEX_DIRS:
        directory = docs / name
        if not directory.exists():
            continue
        index = directory / "index.md"
        if not index.exists():
            add("error", f"缺少索引: {index}")
            continue
        content = index.read_text(encoding="utf-8")
        for file in sorted(directory.glob("*.md")):
            if file.name != "index.md" and file.name not in content:
                add("warning", f"未索引: {file} 不在 {index} 中")

    for spec in REQUIRED_SPECS:
        if not (docs / spec).exists():
            add("error", f"缺少规范: {docs / spec}")
    for file in sorted(docs.glob("*.md")):
        first_lines = file.read_text(encoding="utf-8").splitlines()[:5]
        if not any(re.match(r"^# ", line) for line in first_lines):
            add("warning", f"缺少一级标题: {file}")

    if check_freshness and has_command("git") and try_run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root).ok:
        now = int(time.time())
        for file in markdown_files(docs):
            if file.is_symlink():
                continue
            result = try_run(["git", "log", "-1", "--format=%at", "--", str(file)], cwd=root)
            timestamp = int(result.stdout.strip() or 0) if result.ok else 0
            if timestamp and (age := (now - timestamp) // 86400) > max_age:
                add("warning", f"陈旧 ({age}天): {file}")

    valid_status = re.compile(r"^(verified|stale|draft|—)$")
    for index in (path for path in markdown_files(docs) if path.name == "index.md"):
        lines = index.read_text(encoding="utf-8").splitlines()
        header = next(
            ((i, line.split("|")) for i, line in enumerate(lines) if re.match(r"^\|.*验证状态.*\|", line)), None
        )
        if not header:
            continue
        row_index, columns = header
        status_index = next((i for i, value in enumerate(columns) if "验证状态" in value), -1)
        for line in lines[row_index + 2 :]:
            values = line.split("|")
            if not line.startswith("|") or "_(待" in line or status_index >= len(values):
                continue
            status = values[status_index].strip()
            if status and not valid_status.fullmatch(status):
                add("warning", f"非法状态值 '{status}' in {index}（应为 verified/stale/draft）")

    for kind, directory in (("PRD", docs / "product-specs"), ("技术方案", docs / "tech-docs")):
        if not directory.exists():
            continue
        for file in sorted(directory.glob("*.md")):
            if file.name == "index.md":
                continue
            lines = file.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_DOC_LINES:
                add("error", f"{kind}超限: {file}（{len(lines)} 行，上限 {MAX_DOC_LINES}）")
            if kind == "技术方案":
                disallowed = [
                    f"{i}:{line}"
                    for i, line in enumerate(lines, 1)
                    if re.match(r"^```", line) and not re.match(r"^```(?:mermaid|markdown|text)?$", line, re.I)
                ]
                if disallowed:
                    add("error", f"技术方案含代码示例: {file}（{', '.join(disallowed[:5])}）")

    rules_file = root / "lint" / "task-rules.yml"
    try:
        rules = load_yaml(rules_file).get("doc_section_rules", {}) if rules_file.exists() else {}
    except (OSError, ValueError):
        rules = {}
    if isinstance(rules, dict):
        for directory_name, patterns in rules.items():
            directory = root / directory_name
            if not directory.exists() or not isinstance(patterns, list):
                continue
            for file in sorted(directory.glob("*.md")):
                if file.name == "index.md":
                    continue
                lines = file.read_text(encoding="utf-8").splitlines()
                for pattern in patterns:
                    try:
                        matched = any(re.search(str(pattern), line) for line in lines)
                    except re.error:
                        continue
                    if not matched:
                        add("error", f"{file}: 缺少必填章节匹配 /{pattern}/")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", default="docs")
    parser.add_argument("--max-age", type=int, default=30)
    parser.add_argument("--fix", action="store_true", help="保留兼容；当前检查不改写文档")
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()
    docs = Path(args.docs_dir)
    if not docs.exists():
        parser.error(f"文档目录不存在: {docs}")
    findings = lint_docs(docs, max_age=args.max_age)
    for finding in findings:
        print(f"{'❌' if finding.level == 'error' else '⚠️ '} {finding.message}")
    errors = sum(item.level == "error" for item in findings)
    warnings = sum(item.level == "warning" for item in findings)
    print(f"Harness doc-lint: {errors} 个错误, {warnings} 个警告")
    return 1 if args.ci and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

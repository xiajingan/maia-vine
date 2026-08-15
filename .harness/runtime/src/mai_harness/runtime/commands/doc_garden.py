#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

from mai_harness.runtime.infrastructure.utils import ok, warn

INDEX_DIRS = [
    "product-specs",
    "design-docs",
    "tech-docs",
    "review-reports",
    "test-reports",
    "acceptance-reports",
    "observability-reports",
    "bugs",
]
AI_WORDS = ["赋能", "一站式", "智能化", "全面提升", "极致体验", "无缝衔接"]


def scan(docs: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(level: str, file: Path, message: str) -> None:
        issues.append({"level": level, "file": str(file), "message": message})

    for file in docs.rglob("*.md"):
        text = file.read_text(encoding="utf-8")
        non_empty = sum(bool(line.strip()) for line in text.splitlines())
        headings = re.findall(r"^#{1,3}\s+", text, re.MULTILINE)
        if non_empty > (
            500 if any(part in {"product-specs", "design-docs", "tech-docs"} for part in file.parts) else 800
        ):
            add("medium", file, f"文档 {non_empty} 行，建议拆分或压缩")
        if len(headings) < 2 and file.name != "index.md":
            add("medium", file, "章节过少，难以被索引和渐进式加载准确定位")
        for word in AI_WORDS:
            if word in text:
                add("low", file, f"出现泛化/AI 套话「{word}」，建议改为具体用户动作或约束")
        if re.search(r"TODO|TBD|待补充|占位", text, re.I):
            add("medium", file, "存在 TODO/TBD/占位内容，需确认是否仍有效")
    for name in INDEX_DIRS:
        directory, index = docs / name, docs / name / "index.md"
        if not directory.exists():
            continue
        if not index.exists():
            add("medium", index, "缺少 index.md")
            continue
        text = index.read_text(encoding="utf-8")
        entries = [path for path in directory.glob("*.md") if path.name != "index.md"]
        listed = sum(path.name in text for path in entries)
        if len(entries) > listed:
            add("medium", index, f"索引覆盖 {listed}/{len(entries)}，建议补齐摘要")
        if not re.search(r"摘要|模块|状态|verified|stale|draft|最后更新|Owner|适用范围", text, re.I):
            add("medium", index, "索引缺少摘要/模块/状态信息")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs-dir", default="docs")
    parser.add_argument("--write-report")
    args = parser.parse_args()
    docs = Path(args.docs_dir)
    if not docs.exists():
        raise SystemExit(f"文档目录不存在: {docs}")
    issues = scan(docs)
    counts = {level: sum(item["level"] == level for item in issues) for level in ("high", "medium", "low")}
    report = "\n".join(
        [
            "# Doc Garden Report",
            "",
            f"- docs_dir: {docs}",
            *(f"- {key}: {value}" for key, value in counts.items()),
            "",
            "## Issues",
            "",
            *(f"- **{item['level']}** `{item['file']}` — {item['message']}" for item in issues),
            "",
        ]
    )
    print(report)
    if args.write_report:
        target = Path(args.write_report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
    ok("文档园艺检查未发现问题") if not issues else warn(f"发现 {len(issues)} 个文档园艺建议")


if __name__ == "__main__":
    main()

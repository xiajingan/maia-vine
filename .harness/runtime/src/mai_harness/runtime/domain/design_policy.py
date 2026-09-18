"""Versioned document semantics; execution and human decisions live in application services."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

DESIGN_POLICY_VERSION = 2
HUMAN_DESIGN_TASKS = frozenset({"product", "design", "backend-design", "frontend-design"})
TECHNICAL_TASKS = frozenset({"backend-design", "frontend-design"})
PRODUCT_COLUMNS = ("ID", "系统/模块", "产品行为", "新增复杂度", "必要性/未复用原因", "来源", "验收观察")
PRODUCT_SECTIONS = ("场景与目标", "最小范围与追溯矩阵", "行为与流程", "影响面与保持项")
TECHNICAL_SECTIONS = ("来源与约束", "现状与最小变更", "建模与核心流程")
STATUS_LINE = re.compile(r"(?m)^document_status: (draft|ready|done)\n")
ARCHITECTURE_STATUS_LINE = re.compile(r"(?m)^architecture_implementation_status: (pending|done)\n")


def policy_version(contract: dict[str, Any]) -> int:
    value = contract.get("design_governance_version", 1)
    if type(value) is not int or value not in {1, DESIGN_POLICY_VERSION}:
        raise ValueError("design_governance_version 必须是 1 或 2，已有迭代须显式迁移")
    return value


def document_digest(content: bytes) -> str:
    """Ignore only the one managed lifecycle line in the leading metadata block."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(content).hexdigest()
    header, separator, body = text.partition("\n# ")
    if separator:
        for pattern, field in (
            (STATUS_LINE, "document_status"),
            (ARCHITECTURE_STATUS_LINE, "architecture_implementation_status"),
        ):
            if len(pattern.findall(header + "\n")) == 1:
                header = pattern.sub(f"{field}: <managed>\n", header + "\n").removesuffix("\n")
        content = (header + separator + body).encode()
    return hashlib.sha256(content).hexdigest()


def file_digest(path: Path) -> str:
    return document_digest(path.read_bytes())


def document_status(content: str) -> str | None:
    header, separator, _ = content.partition("\n# ")
    matches = STATUS_LINE.findall(header + "\n") if separator else []
    if len(matches) != 1 or len(re.findall(r"(?m)^document_status:", content)) != 1:
        return None
    return matches[0]


def with_document_status(content: str, status: str) -> str:
    if status not in {"draft", "ready", "done"} or document_status(content) is None:
        raise ValueError("文档必须在标题前声明唯一 document_status: draft/ready/done")
    return STATUS_LINE.sub(f"document_status: {status}\n", content, count=1)


def validate_sections(content: str, required: tuple[str, ...], label: str) -> list[str]:
    """Check the small document shape, never infer semantic quality from text or diagrams."""
    headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", content))
    names = tuple(match.group(1) for match in headings)
    errors = []
    if names != required:
        errors.append(f"{label} 必须按顺序且仅有核心二级章节: {'、'.join(required)}")
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        body = re.sub(r"<!--.*?-->", "", content[heading.end() : end], flags=re.S).strip()
        if not body or re.fullmatch(r"(?i)(?:TODO|TBD|待补|待定|占位)[。.!\s]*", body):
            errors.append(f"{label} 核心章节『{heading.group(1)}』不能为空或使用占位内容")
    return errors

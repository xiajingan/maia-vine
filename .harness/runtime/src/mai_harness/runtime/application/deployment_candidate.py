"""Validate deployment source approvals against one immutable candidate commit."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from mai_harness.runtime.domain.sprint_context import header_field, header_list_field
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run

SPRINT_ID = re.compile(r"sprint-\d+-[a-z0-9][a-z0-9-]*")
COMMIT = re.compile(r"[a-f0-9]{40}")


@dataclass
class CandidateValidation:
    candidate_commit: str = ""
    source_signoffs: list[dict[str, str]] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_deployment_candidate(
    root: Path,
    content: str,
    *,
    source_field: str,
    candidate_field: str = "",
    require_head_match: bool = False,
    require_source_ancestry: bool = False,
) -> CandidateValidation:
    """Return exact signoff snapshots and fail-closed candidate validation."""

    result = CandidateValidation(candidate_commit=header_field(content, candidate_field) if candidate_field else "")
    source_sprints = header_list_field(content, source_field)
    if not source_sprints:
        result.errors.append(f"Sprint 计划缺少非空 {source_field}: [...] 输入")
    elif len(source_sprints) != len(set(source_sprints)):
        result.errors.append(f"{source_field} 含重复 Sprint")
    else:
        result.passed.extend((f"已声明 {source_field}", f"{source_field} 无重复"))

    candidate_exists = False
    if candidate_field:
        candidate_exists = bool(
            COMMIT.fullmatch(result.candidate_commit)
            and try_run(("git", "cat-file", "-e", f"{result.candidate_commit}^{{commit}}"), cwd=root).ok
        )
        if candidate_exists:
            result.passed.append(f"部署候选 commit 有效: {result.candidate_commit}")
        else:
            result.errors.append(f"Sprint 计划缺少有效 {candidate_field}: {result.candidate_commit or '缺失'}")
        if require_head_match and candidate_exists:
            head = try_run(("git", "rev-parse", "HEAD"), cwd=root)
            current = head.stdout.strip() if head.ok else ""
            if current == result.candidate_commit:
                result.passed.append("当前 HEAD 与部署候选 commit 一致")
            else:
                result.errors.append(
                    f"当前 HEAD 与部署候选 commit 不一致: {current or '无法读取'} != {result.candidate_commit}"
                )

    for source_sprint in source_sprints:
        if not SPRINT_ID.fullmatch(source_sprint):
            result.errors.append(f"{source_field} 含非法 Sprint ID: {source_sprint}")
            continue
        signoff = root / "docs/acceptance-reports" / f"{source_sprint}-boss-signoff.yml"
        if not signoff.is_file():
            result.errors.append(f"源 Sprint 审批缺失: {signoff}")
            continue
        try:
            loaded = load_yaml(signoff)
        except Exception as exc:  # YAML/parser failures are untrusted input failures.
            result.errors.append(f"源 Sprint 审批无法读取: {signoff}: {exc}")
            continue
        if not isinstance(loaded, dict):
            result.errors.append(f"源 Sprint 审批格式非法（顶层必须是对象）: {signoff}")
            continue
        commit = str(loaded.get("commit_sha", "")).strip()
        if loaded.get("decision") != "approved":
            result.errors.append(f"源 Sprint 未批准: {source_sprint}")
        if loaded.get("sprint") != source_sprint:
            result.errors.append(f"源 Sprint 审批身份不匹配: {loaded.get('sprint', '缺失')} != {source_sprint}")
        commit_exists = bool(
            COMMIT.fullmatch(commit) and try_run(("git", "cat-file", "-e", f"{commit}^{{commit}}"), cwd=root).ok
        )
        if not commit_exists:
            result.errors.append(f"源 Sprint commit 无效: {commit or '缺失'}")
        elif require_source_ancestry and candidate_exists:
            if try_run(("git", "merge-base", "--is-ancestor", commit, result.candidate_commit), cwd=root).ok:
                result.passed.append(f"源 Sprint commit 已纳入部署候选: {source_sprint}")
            else:
                result.errors.append(
                    f"源 Sprint commit 未纳入部署候选: {commit} !<= {result.candidate_commit}"
                )
        if (
            loaded.get("decision") == "approved"
            and loaded.get("sprint") == source_sprint
            and commit_exists
        ):
            result.passed.extend((f"源 Sprint 已批准: {source_sprint}", f"源 Sprint commit 有效: {commit}"))
            result.source_signoffs.append(
                {
                    "sprint": source_sprint,
                    "path": signoff.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(signoff.read_bytes()).hexdigest(),
                    "commit_sha": commit,
                }
            )
    return result

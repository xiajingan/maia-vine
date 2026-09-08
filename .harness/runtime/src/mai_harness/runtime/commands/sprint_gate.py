#!/usr/bin/env python3
"""Execute task-rules.yml pre-flight gates for a Sprint task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mai_harness.runtime.application.action_executor import action_argv, execute_action
from mai_harness.runtime.application.dependency_session import validate_session
from mai_harness.runtime.application.deployment_candidate import validate_deployment_candidate
from mai_harness.runtime.application.integration_contract import (
    delivery_identity,
    integration_contract,
    validate_integration_contract,
)
from mai_harness.runtime.application.sprint_context import (
    validate_sprint_activation,
    validate_sprint_context,
)
from mai_harness.runtime.application.task_evidence import (
    activate_attempt,
    current_attempt_state_path,
    ensure_attempt,
    record_phase,
    validate_attempt,
    validate_failed_action_evidence,
)
from mai_harness.runtime.commands.validate_task_rules import validate as validate_rules
from mai_harness.runtime.domain.actions import resolve_action
from mai_harness.runtime.domain.modes import PROJECT_TYPES
from mai_harness.runtime.domain.sprint_context import (
    header_field,
    planning_contract_digest,
    sprint_planning_contract,
    sprint_structure_digest,
    table_rows,
    task_dependency_graph,
    transitive_task_dependencies,
    validate_sprint_outcome_evidence,
)
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config, resolve_delivery_ref
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run

DONE = re.compile(r"^(done|完成|通过)$", re.I)
ROLLBACK = re.compile(r"^(rollback|回退)$", re.I)
COMPLETED_PLAN_TASK_TYPES = frozenset({"pr", "library-pr"})


def allows_completed_plan(task_type: str, phase: str = "preflight") -> bool:
    """Return whether a post-archive task may read a completed Sprint plan."""

    return task_type in COMPLETED_PLAN_TASK_TYPES or (
        phase == "review" and task_type in {"sprint-close", "library-close"}
    )


@dataclass
class GateResult:
    passed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def check(self, condition: bool, success: str, failure: str) -> None:
        (self.passed if condition else self.blocked).append(success if condition else failure)


def load_mapping_evidence(path: Path, result: GateResult, label: str) -> dict[str, Any]:
    """Read untrusted YAML evidence and convert parse/shape failures into BLOCKED."""
    try:
        loaded = load_yaml(path)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        result.blocked.append(f"{label}无法读取: {path}: {exc}")
        return {}
    if not isinstance(loaded, dict):
        result.blocked.append(f"{label}格式非法（顶层必须是对象）: {path}")
        return {}
    return loaded


def validate_signoff_delivery_refs(
    root: Path,
    config: dict[str, Any],
    commit: str,
    result: GateResult,
) -> None:
    """Verify the approved commit reached the configured delivery targets."""

    keys = ("development", "test") if config.get("walkthrough_env") == "test" else ("development",)
    try:
        resolved = [resolve_delivery_ref(config, key) for key in keys]
    except ValueError as exc:
        result.blocked.append(str(exc))
        return
    remote = resolved[0][0]
    branches = [branch for _, branch, _ in resolved]
    fetch = try_run(["git", "fetch", remote, *branches, "--quiet"], cwd=root)
    result.check(fetch.ok, "目标分支引用已刷新", f"无法刷新远端目标分支引用: {remote}")
    if not fetch.ok:
        return
    for _, _, ref in resolved:
        contained = try_run(["git", "merge-base", "--is-ancestor", commit, ref], cwd=root)
        result.check(contained.ok, f"signoff commit_sha 已抵达 {ref}", f"signoff commit_sha 未抵达 {ref}: {commit}")


def validate_ui_design_approval(
    root: Path,
    sprint_file: Path,
    task_rows: list[tuple[str, str, str]],
    record: dict[str, Any],
    result: GateResult,
) -> None:
    """Bind Boss UI approval to the current planning contract and reviewed design task."""
    sprint_id = sprint_file.stem
    result.check(record.get("sprint") == sprint_id, "UI 审批 Sprint 匹配", "UI 审批未绑定当前 Sprint")
    result.check(
        record.get("planning_contract_sha256") == planning_contract_digest(sprint_file),
        "UI 审批规划摘要匹配",
        "UI 审批未绑定当前 planning contract",
    )
    for approval_field in ("confirmed_by", "confirmed_at", "source"):
        result.check(
            bool(record.get(approval_field)),
            f"UI 审批包含 {approval_field}",
            f"UI 审批缺少 {approval_field}",
        )
    expected_reviews: list[dict[str, str]] = []
    design_rows = [(task_id, status) for task_id, task_type, status in task_rows if task_type == "design"]
    for task_id, status in design_rows:
        if not task_id or not DONE.match(status):
            result.blocked.append(f"UI L3 前 design 任务必须有 ID 且完成: {task_id or '缺失 ID'}")
            continue
        state_path = current_attempt_state_path(root, sprint_file, task_id)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result.blocked.append(f"UI L3 无法读取 design attempt: {state_path}: {exc}")
            continue
        review = state.get("review") if isinstance(state, dict) else None
        context = state.get("context") if isinstance(state, dict) else None
        if (
            state.get("schema_version") != 3
            or state.get("task_type") != "design"
            or state.get("status") != "ready"
            or not isinstance(review, dict)
            or review.get("decision") != "pass"
            or not isinstance(context, dict)
            or context.get("sprint_structure_sha256") != sprint_structure_digest(sprint_file)
        ):
            result.blocked.append(f"UI L3 design Review 未通过当前 attempt: {task_id}")
            continue
        report_relative = Path(str(review.get("report", "")))
        report_path = (root / report_relative).resolve()
        try:
            report_safe = (
                not report_relative.is_absolute()
                and ".." not in report_relative.parts
                and report_path.is_relative_to(root.resolve())
                and report_path.is_file()
                and review.get("report_sha256") == hashlib.sha256(report_path.read_bytes()).hexdigest()
            )
        except OSError:
            report_safe = False
        artifacts = review.get("artifacts")
        artifacts_safe = isinstance(artifacts, list) and bool(artifacts)
        for item in artifacts or []:
            if not isinstance(item, dict) or not {"path", "sha256"} <= set(item):
                artifacts_safe = False
                break
            artifact_relative = Path(str(item["path"]))
            artifact_path = (root / artifact_relative).resolve()
            try:
                if (
                    artifact_relative.is_absolute()
                    or ".." in artifact_relative.parts
                    or not artifact_path.is_relative_to(root.resolve())
                    or not artifact_path.is_file()
                    or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != item["sha256"]
                ):
                    artifacts_safe = False
                    break
            except OSError:
                artifacts_safe = False
                break
        if not report_safe or not artifacts_safe:
            result.blocked.append(f"UI L3 design Review 报告或 artifact 已漂移: {task_id}")
            continue
        expected_reviews.append(
            {
                "task_id": task_id,
                "report": str(review.get("report", "")),
                "sha256": str(review.get("report_sha256", "")),
                "recorded_at": str(review.get("recorded_at", "")),
            }
        )
    result.check(
        bool(expected_reviews) and record.get("design_reviews") == expected_reviews,
        "UI 审批绑定当前 design Review",
        "UI 审批 design_reviews 与当前 Review 不一致",
    )
    commit_sha = str(record.get("commit_sha", ""))
    commit_valid = (
        bool(re.fullmatch(r"[0-9a-f]{40,64}", commit_sha))
        and try_run(("git", "merge-base", "--is-ancestor", commit_sha, "HEAD"), cwd=root).ok
    )
    result.check(commit_valid, "UI 审批提交仍在当前 lineage", "UI 审批 commit_sha 无效或不属于当前 HEAD")


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_task_rows(content: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        headers = split_row(line)
        type_index = next((i for i, value in enumerate(headers) if re.match(r"^(类型|type)$", value, re.I)), -1)
        status_index = next((i for i, value in enumerate(headers) if re.match(r"^(状态|status)$", value, re.I)), -1)
        id_index = next((i for i, value in enumerate(headers) if re.match(r"^(ID|任务ID|task id)$", value, re.I)), -1)
        if (
            min(type_index, status_index) < 0
            or index + 1 >= len(lines)
            or not all(re.match(r"^:?-{3,}:?$", cell) for cell in split_row(lines[index + 1]))
        ):
            continue
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|"):
                break
            cells = split_row(row)
            if len(cells) <= max(type_index, status_index):
                continue
            task_id = cells[id_index] if id_index >= 0 and id_index < len(cells) else ""
            rows.append((task_id, cells[type_index], cells[status_index]))
    return rows


def parse_task_statuses(content: str) -> dict[str, list[str]]:
    """Return statuses keyed only by task type; IDs are a separate namespace."""
    statuses: dict[str, list[str]] = {}
    for _, task_type, status in parse_task_rows(content):
        statuses.setdefault(task_type, []).append(status)
    return statuses


def parse_task_id_statuses(content: str) -> dict[str, str]:
    return {task_id: status for task_id, _, status in parse_task_rows(content) if task_id}


def task_keyword(text: str, task_names: list[str]) -> str:
    value = str(text)
    known = next(
        (key for key in task_names if re.search(rf"(?<![A-Za-z0-9-]){re.escape(key)}(?![A-Za-z0-9-])", value)),
        "",
    )
    if known:
        return known
    fallback = re.match(r"^([a-z][a-z0-9-]+)\b", value)
    return fallback.group(1) if fallback else ""


def task_status(keyword: str, content: str, statuses: dict[str, list[str]], result: GateResult) -> str:
    found = statuses.get(keyword, [])
    if found:
        return (
            "done"
            if all(DONE.match(status) for status in found)
            else next((status for status in found if not DONE.match(status)), found[-1])
        )
    matches = [
        match.group(1)
        for line in content.splitlines()
        if re.search(re.escape(keyword), line, re.I)
        if (match := re.search(r"\b(done|完成|通过|in-progress|pending|blocked|rollback|回退)\b", line, re.I))
    ]
    return matches[-1] if matches else ""


def stage_tasks(stage: Any) -> list[str]:
    return list(stage.get("tasks", [])) if isinstance(stage, dict) else list(stage)


def fill_pattern(value: str, sprint_id: str) -> str:
    match = re.match(r"^sprint-\d+", sprint_id)
    series = match.group(0) if match else sprint_id
    return value.replace("sprint-N-name", sprint_id).replace("sprint-N", series).replace("<N-name>", sprint_id)


def retry_gate(
    root: Path,
    sprint_id: str,
    task_id: str,
    max_retry: int,
    action: str | None,
    result: GateResult,
    reason: str = "",
) -> None:
    directory = root / ".harness/retry"
    counter = directory / f"{sprint_id}-{task_id}.count"
    audit = directory / f"{sprint_id}-{task_id}.audit.log"
    try:
        current = int(counter.read_text().strip()) if counter.exists() else 0
    except ValueError:
        current = 0
    if action:
        before = current
        current = 0 if action == "reset" else current + 1
        directory.mkdir(parents=True, exist_ok=True)
        counter.write_text(f"{current}\n", encoding="utf-8")
        with audit.open("a", encoding="utf-8") as stream:
            suffix = f" reason={reason}" if reason else ""
            stream.write(
                f"{datetime.now(UTC).isoformat()} {'RESET' if action == 'reset' else 'INCREMENT'} "
                f"({before} → {current}){suffix}\n"
            )
    result.check(
        current <= max_retry,
        f"重试计数: {current}/{max_retry}",
        f"任务 {sprint_id}-{task_id} 已重试 {current} 次（> {max_retry}）",
    )


def run_cached_command(
    command: tuple[str, ...], cache_file: Path, cache_key: str, ttl: int, root: Path, timeout: int
) -> tuple[bool, str]:
    store = StateStore(cache_file.parent)
    try:
        cache = store.read_json(cache_file.name, {})
    except ValueError:
        cache = {}
    now = int(time.time())
    if cache.get(cache_key) and now - cache[cache_key] < ttl:
        return True, f"TTL 内复用（{now - cache[cache_key]}s）"
    execution = try_run(command, cwd=root, timeout=timeout)
    if execution.ok:
        cache[cache_key] = now
        store.write_json(cache_file.name, cache)
    return execution.ok, (execution.stdout or execution.stderr).strip()[-2000:]


def infra_cache_key(root: Path, task_type: str, action_id: str, command: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(command, ensure_ascii=False).encode())
    digest.update(json.dumps(dict(sorted(os.environ.items())), ensure_ascii=False).encode())
    for relative in ("config/harness.yml", "config/deploy.yml", ".harness/rules/task-rules.yml"):
        path = root / relative
        if path.is_file():
            digest.update(relative.encode())
            digest.update(path.read_bytes())
    git = try_run(("git", "rev-parse", "HEAD"), cwd=root)
    digest.update(git.stdout.strip().encode() if git.ok else b"unversioned")
    return f"{task_type}::{action_id}::{digest.hexdigest()}"


def evaluate(
    task_type: str,
    sprint_file: Path,
    rules: dict[str, Any],
    root: Path,
    *,
    strict: bool = False,
    run_commands: bool = True,
    phase: str = "preflight",
    ttl: int = 600,
    rules_path: Path | None = None,
    task_id: str | None = None,
) -> GateResult:
    result = GateResult()
    task = (rules.get("tasks") or {}).get(task_type)
    if task is None:
        result.blocked.append(f"task-rules.yml 中未找到任务类型: {task_type}")
        return result
    capability_matrix = rules.get("mode_task_capabilities") or {}
    allowed_modes = (
        {
            mode
            for mode, task_types in capability_matrix.items()
            if isinstance(task_types, list) and task_type in task_types
        }
        if capability_matrix
        else set(task.get("allowed_modes") or {"standalone", "managed", "control"})
    )
    if capability_matrix and task.get("allowed_modes") and allowed_modes != set(task["allowed_modes"]):
        result.blocked.append(f"任务 {task_type} 的 allowed_modes 与 mode_task_capabilities 不一致")
        return result
    try:
        harness_config = load_harness_config()
        current_mode = harness_config["project"]["mode"]
    except (FileNotFoundError, ValueError) as exc:
        result.blocked.append(f"Harness mode 无法验证: {exc}")
        return result
    if current_mode not in allowed_modes:
        result.blocked.append(f"任务 {task_type} 不允许用于 mode={current_mode}")
        return result
    allowed_project_types = set(task.get("allowed_project_types") or PROJECT_TYPES)
    current_type = harness_config["project"].get("type", "fullstack")
    if current_type not in allowed_project_types:
        result.blocked.append(f"任务 {task_type} 不允许用于 project.type={current_type}")
        return result
    content = sprint_file.read_text(encoding="utf-8")
    task_names = sorted((rules.get("tasks") or {}), key=len, reverse=True)
    task_rows = parse_task_rows(content)
    statuses = parse_task_statuses(content)
    type_capabilities = rules.get("sprint_type_task_capabilities") or {}
    sprint_type = ""
    if type_capabilities:
        sprint_type_match = re.search(r"(?m)^\s*sprint_type\s*:\s*([a-z0-9-]+)\s*$", content)
        if not sprint_type_match:
            result.blocked.append("Sprint 计划缺少结构化 sprint_type")
            return result
        sprint_type = sprint_type_match.group(1)
        mode_types = (rules.get("sprint_type_mode_capabilities") or {}).get(current_mode, [])
        if sprint_type not in mode_types:
            result.blocked.append(f"sprint_type={sprint_type} 不允许用于 mode={current_mode}")
            return result
        if sprint_type not in type_capabilities:
            result.blocked.append(f"未知 sprint_type={sprint_type}")
            return result
        current_type = harness_config["project"]["type"]
        allowed_project_types = set((rules.get("sprint_type_project_types") or {}).get(sprint_type, []))
        if allowed_project_types and current_type not in allowed_project_types:
            result.blocked.append(f"sprint_type={sprint_type} 不允许用于 project.type={current_type}")
            return result
        if task_type not in type_capabilities[sprint_type]:
            result.blocked.append(f"任务 {task_type} 不允许用于 sprint_type={sprint_type}")
            return result
        sequences = rules.get("sprint_type_sequences") or {}
        stages = sequences.get(sprint_type, [])
        stage_index = next((index for index, stage in enumerate(stages) if task_type in stage_tasks(stage)), None)
        if stage_index is None:
            result.blocked.append(f"任务 {task_type} 未登记到 sprint_type={sprint_type} 的执行序列")
            return result
        duplicate_ids = sorted(
            {row_id for row_id, _, _ in task_rows if row_id and sum(r[0] == row_id for r in task_rows) > 1}
        )
        if duplicate_ids:
            result.blocked.append(f"Sprint 任务 ID 重复: {', '.join(duplicate_ids)}")
            return result
        reserved_ids = sorted({row_id for row_id, _, _ in task_rows if row_id} & set(task_names))
        if reserved_ids:
            result.blocked.append(f"Sprint 任务 ID 不得与任务类型同名: {', '.join(reserved_ids)}")
            return result
        matching_rows = [row for row in task_rows if row[1] == task_type and (not task_id or row[0] == task_id)]
        if len(matching_rows) != 1:
            result.blocked.append(f"当前任务未登记到 Sprint 任务表: {task_id or task_type} ({task_type})")
            return result
    if task.get("execution_contract") == "integration-v1":
        contract = integration_contract(sprint_file, task_id or task_type, task)
        result.blocked.extend(validate_integration_contract(contract, task, harness_config))
        identity, identity_errors = delivery_identity(root, sprint_file.stem)
        result.blocked.extend(identity_errors)
        if not identity_errors:
            result.passed.append(f"integration 已绑定 build/deploy 身份: {identity['build_state']['source_commit']}")
    sprint_id = sprint_file.stem
    if type_capabilities:
        for earlier_stage in stages[:stage_index]:
            declared = stage_tasks(earlier_stage)
            applicable = [
                name
                for name in declared
                if current_type
                in set((rules.get("tasks", {}).get(name) or {}).get("allowed_project_types") or PROJECT_TYPES)
            ]
            if not applicable:
                continue
            present = [name for name in applicable if name in statuses]
            optional = isinstance(earlier_stage, dict) and earlier_stage.get("optional") is True
            if not present and optional:
                continue
            if not present:
                result.blocked.append(f"前序阶段未列入 Sprint: {', '.join(applicable)}")
                continue
            outcome = task.get("upstream_outcome") or {}
            expected = (
                {str(status).lower() for status in outcome.get("statuses", [])}
                if outcome.get("task") in present
                else set()
            )
            requirement = earlier_stage.get("require", "all") if isinstance(earlier_stage, dict) else "all"
            completed = {
                name
                for name in present
                if (
                    all(status.lower() in expected for status in statuses[name])
                    if name == outcome.get("task")
                    else all(DONE.match(status) for status in statuses[name])
                )
            }
            # Optional stages are selected by the Sprint's declared impact surface:
            # zero tasks may be omitted, while every task that is explicitly planned
            # must still complete. Non-optional stages continue to require every
            # project-applicable task from the sequence.
            if requirement == "all" and not optional and set(present) != set(applicable):
                missing = [name for name in applicable if name not in present]
                result.blocked.append(f"前序阶段任务未列入 Sprint(all): {', '.join(missing)}")
                continue
            satisfied = bool(completed) if requirement == "any" else len(completed) == len(present)
            if present and not satisfied:
                result.blocked.append(f"前序阶段未完成({requirement}): {', '.join(present)}")
                continue
            if sprint_planning_contract(sprint_file).get("planning_contract_version") == 2:
                evidence_rules_path = rules_path or HarnessPaths.detect(project=root).rules / "task-rules.yml"
                for completed_type in sorted(completed):
                    completed_rows = [row for row in task_rows if row[1] == completed_type]
                    if completed_type == outcome.get("task") and not all(DONE.match(row[2]) for row in completed_rows):
                        continue
                    for source_id, source_type, _ in completed_rows:
                        if not source_id:
                            result.blocked.append(f"前序任务缺少 ID，无法验证 attempt: {source_type}")
                            continue
                        source_task = (rules.get("tasks") or {}).get(source_type, {})
                        evidence_errors = validate_attempt(
                            root,
                            sprint_file,
                            evidence_rules_path,
                            source_id,
                            source_type,
                            source_task,
                            upstream=True,
                        )
                        result.blocked.extend(
                            f"前序任务 {source_id} ({source_type}) 当前 attempt 无效: {error}"
                            for error in evidence_errors
                        )
        if sprint_planning_contract(sprint_file).get("planning_contract_version") == 3 and task_id:
            structured_rows = table_rows(content)
            dependency_graph, dependency_errors = task_dependency_graph(structured_rows)
            result.blocked.extend(dependency_errors)
            dependencies = transitive_task_dependencies(dependency_graph, task_id)
            rows_by_id = {str(row.get("id")): row for row in structured_rows if row.get("id")}
            evidence_rules_path = rules_path or HarnessPaths.detect(project=root).rules / "task-rules.yml"
            for source_id in sorted(dependencies):
                source_row = rows_by_id.get(source_id, {})
                source_type = source_row.get("类型") or source_row.get("type") or ""
                source_status = source_row.get("状态") or source_row.get("status") or ""
                if not DONE.match(source_status):
                    result.blocked.append(
                        f"显式依赖任务 {source_id} ({source_type}) 未完成: {source_status or '未找到'}"
                    )
                    continue
                source_task = (rules.get("tasks") or {}).get(source_type, {})
                evidence_errors = validate_attempt(
                    root,
                    sprint_file,
                    evidence_rules_path,
                    source_id,
                    source_type,
                    source_task,
                    upstream=True,
                )
                result.blocked.extend(
                    f"显式依赖任务 {source_id} ({source_type}) 当前 attempt 无效: {error}" for error in evidence_errors
                )
    for prerequisite in task.get("prerequisites", []):
        keyword = task_keyword(prerequisite, task_names)
        if keyword:
            # 类型序列没有包含的任务属于其他 Sprint；其结果应由本任务的输入/制品门禁验证，
            # 不能要求在当前 Sprint 重复执行。
            if type_capabilities and not any(keyword in stage_tasks(stage) for stage in stages):
                result.passed.append(f"跨 Sprint 前置由输入门禁承担: {prerequisite}")
                continue
            status = task_status(keyword, content, statuses, result)
            result.check(
                bool(DONE.match(status)),
                f"前置条件满足: {prerequisite}",
                f"前置条件未满足: {prerequisite} (状态: {status or '未找到'})",
            )
        else:
            match = re.search(r"[A-Z_]+\.md", str(prerequisite))
            if match:
                result.check(
                    any(root.glob(f"**/{match.group()}")),
                    f"前置文件存在: {match.group()}",
                    f"前置文件不存在: {match.group()}",
                )
            else:
                result.warnings.append(f"前置条件需人工确认: {prerequisite}")
    satisfied_any: list[str] = []
    for group in task.get("prerequisites_any", []):
        local_group = [
            key for key in group if not type_capabilities or any(key in stage_tasks(stage) for stage in stages)
        ]
        if not local_group:
            result.passed.append(f"跨 Sprint 任一前置由输入门禁承担: {', '.join(group)}")
            continue
        matched = next((key for key in local_group if DONE.match(task_status(key, content, statuses, result))), "")
        result.check(bool(matched), f"任一前置条件满足: {matched}", f"任一前置条件组未满足: {', '.join(local_group)}")
        if matched:
            satisfied_any.append(matched)
    spawned = []
    for rule in rules.get("spawn_rules", []):
        targets = rule.get("to", [])
        targets = targets if isinstance(targets, list) else [targets]
        sources = rule.get("from", [])
        sources = sources if isinstance(sources, list) else [sources]
        if task_type in targets:
            source_statuses = {source: task_status(source, content, statuses, result) for source in sources}
            completed = [source for source, status in source_statuses.items() if DONE.match(status)]
            requirement = rule.get("require", "all")
            satisfied = bool(completed) if requirement == "any" else len(completed) == len(sources)
            if satisfied:
                result.passed.append(f"派生来源已完成: {', '.join(completed)}")
            spawned.extend(completed)
    upstream = list(
        dict.fromkeys(
            [task_keyword(item, task_names) for item in task.get("prerequisites", []) if task_keyword(item, task_names)]
            + satisfied_any
            + spawned
            + ([task["upstream_outcome"]["task"]] if task.get("upstream_outcome") else [])
        )
    )
    outcome = task.get("upstream_outcome") or {}
    outcome_statuses = {str(status).lower() for status in outcome.get("statuses", [])}
    outcome_source_rows = [row for row in task_rows if row[1] == outcome.get("task")] if outcome.get("task") else []
    outcome_source_statuses = statuses.get(outcome.get("task"), []) if outcome.get("task") else []
    outcome_satisfied = (
        len(outcome_source_rows) == 1
        and bool(outcome_source_statuses)
        and all(status.lower() in outcome_statuses for status in outcome_source_statuses)
    )
    if outcome.get("task") and not outcome_satisfied:
        result.blocked.append(f"upstream_outcome 要求唯一 {outcome.get('task')} 且状态属于 {sorted(outcome_statuses)}")
    outcome_failed = outcome_satisfied and not all(DONE.match(status) for status in outcome_source_statuses)
    if outcome.get("require_action_evidence") is True and outcome_failed:
        for source_id, source_type, _ in outcome_source_rows:
            source_task = (rules.get("tasks") or {}).get(source_type, {})
            source_action = (source_task.get("execute") or {}).get("action")
            result.blocked.extend(
                validate_failed_action_evidence(
                    root,
                    sprint_file,
                    rules_path or HarnessPaths.detect(project=root).rules / "task-rules.yml",
                    source_id,
                    source_type,
                    source_task,
                    source_action,
                )
            )
    external = task.get("external_evidence") or {}
    external_sprint_types = external.get("sprint_types")
    external_applies = external_sprint_types is None or sprint_type in external_sprint_types
    if external.get("kind") == "sprint-signoffs" and external_applies:
        validation = validate_deployment_candidate(
            root,
            content,
            source_field=external.get("field", "source_sprints"),
            candidate_field=external.get("candidate_field", ""),
            require_head_match=external.get("require_head_match") is True,
            require_source_ancestry=external.get("require_source_ancestry") is True,
        )
        result.passed.extend(validation.passed)
        result.blocked.extend(validation.errors)
    for name in upstream:
        definition = (rules.get("tasks") or {}).get(name, {})
        output = definition.get("outputs", {}).get("path", "")
        if output and not re.search(r"项目根目录|PR URL|部署产物", output):
            path = root / re.split(r"\s*或\s*", output)[0].strip()
            valid = path.exists() and (path.is_file() or any(path.rglob("*")))
            if strict:
                result.check(valid, f"上游 {name}: 产出物存在", f"上游 {name}: 产出物缺失或为空 ({path})")
            elif not valid:
                result.warnings.append(f"上游 {name}: 产出物缺失或为空 ({path})")
        if run_commands:
            for command in definition.get("completion_checks", []):
                actual = fill_pattern(command, sprint_id)
                execution = try_run(actual, cwd=root)
                if strict:
                    result.check(
                        execution.ok,
                        f"上游 {name}: completion_check 通过",
                        f"上游 {name}: completion_check 未通过: {actual}",
                    )
                elif not execution.ok:
                    result.warnings.append(f"上游 {name}: completion_check 未通过: {actual}")
    report = (task.get("readiness") or {}).get("quality_report")
    if report:
        path = root / fill_pattern(report["path"], sprint_id)
        result.check(path.exists(), f"质量报告存在: {path}", f"质量报告不存在: {path}")
        if path.exists():
            text = path.read_text(encoding="utf-8")
            for marker in report.get("markers", []):
                try:
                    matched = bool(re.search(marker, text))
                except re.error:
                    matched = marker in text
                result.check(matched, f"质量信号满足: {marker}", f"质量信号缺失: {marker}")
    approval = task.get("approval_artifact")
    approval_phase = task.get("approval_phase", "all")
    approval_record: dict[str, Any] = {}
    approval_loaded = False
    if approval and (approval_phase == "all" or approval_phase == phase):
        path = root / fill_pattern(approval, sprint_id)
        result.check(path.exists(), f"审批记录存在: {path}", f"审批记录不存在: {path}")
        if path.exists():
            approval_loaded = True
            approval_record = load_mapping_evidence(path, result, "审批记录")
            result.check(
                approval_record.get("decision") == "approved",
                "审批记录状态为 approved",
                "审批记录格式非法或未放行",
            )
    for declaration in task.get("preflight_file_checks", []):
        relative = re.sub(r"\s*必须存在.*$", "", declaration).strip()
        path = root / fill_pattern(relative, sprint_id)
        result.check(path.exists(), f"产出物存在: {relative}", f"产出物缺失: {relative}")
    state_key = {"promote-test": "success"}.get(task_type)
    if state_key:
        state_path = root / f".harness/state/build-image-{sprint_id}.json"
        if not state_path.exists():
            series_match = re.match(r"^sprint-\d+", sprint_id)
            series = series_match.group(0) if series_match else sprint_id
            candidates = (
                sorted(
                    (root / ".harness/state").glob(f"build-image-{series}*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                if (root / ".harness/state").exists()
                else []
            )
            if len(candidates) == 1:
                state_path = candidates[0]
            elif len(candidates) > 1:
                result.blocked.append(f"build-image 状态文件不唯一：{', '.join(str(path) for path in candidates)}")
        result.check(state_path.exists(), f"部署状态文件存在: {state_path}", f"部署状态文件缺失: {state_path}")
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if task_type == "promote-test":
                    result.check(
                        state.get("sprint") == sprint_id,
                        "部署状态 Sprint 匹配",
                        f"部署状态 sprint 不匹配: {state.get('sprint')} != {sprint_id}",
                    )
                    head = try_run(("git", "rev-parse", "HEAD"), cwd=root)
                    current_head = head.stdout.strip() if head.ok else ""
                    result.check(
                        bool(current_head) and state.get("source_commit") == current_head,
                        "build-image 来源 commit 与当前部署候选一致",
                        "build-image 来源 commit 缺失或已偏离当前 HEAD",
                    )
                    if sprint_type == "deploy-sprint-test":
                        validation = validate_deployment_candidate(
                            root,
                            content,
                            source_field="source_sprints",
                            candidate_field="base_sha",
                            require_head_match=True,
                            require_source_ancestry=True,
                        )
                        result.passed.extend(validation.passed)
                        result.blocked.extend(validation.errors)
                        result.check(
                            state.get("candidate_commit") == validation.candidate_commit
                            and state.get("source_signoffs") == validation.source_signoffs,
                            "build-image 已绑定当前部署候选和源 Sprint 审批摘要",
                            "build-image 未绑定当前部署候选，或源 Sprint 审批在构建后发生变化",
                        )
                result.check(
                    state.get(state_key) is True,
                    f"部署状态门控通过: {state_key}=true",
                    f"部署状态门控失败: {state_key} != true",
                )
            except json.JSONDecodeError as exc:
                result.blocked.append(f"部署状态 JSON 解析失败: {state_path}: {exc}")
    config: dict[str, Any] = {}
    try:
        config = load_harness_config()
        match = re.match(r"^sprint-\d+", sprint_id)
        series = match.group(0) if match else sprint_id
        if task_type == "code" and config.get("gates", {}).get("ui_design_l3") is True and "design" in statuses:
            approval_path = root / "docs/design-docs" / f"{series}-design-approval.yml"
            design_approval = (
                load_mapping_evidence(approval_path, result, "UI Design L3 审批") if approval_path.exists() else {}
            )
            result.check(
                approval_path.exists() and design_approval.get("decision") == "approved",
                "UI Design L3 审批通过",
                f"UI Design L3 审批缺失或未放行: {approval_path}",
            )
            if design_approval.get("decision") == "approved":
                validate_ui_design_approval(root, sprint_file, task_rows, design_approval, result)
        if (
            task_type in {"quality", "product-acceptance", "pr", "sprint-close"}
            and config.get("walkthrough_env") == "test"
            and "code" in statuses
        ):
            required = (
                "build-image",
                "promote-test",
                "integration",
                "quality",
                "product-acceptance",
                "pr",
                "sprint-close",
            )
            missing = [
                name
                for name in required
                if name not in statuses
                and not re.search(rf"(^|[^a-z0-9-]){re.escape(name)}([^a-z0-9-]|$)", content, re.I)
            ]
            result.check(
                not missing,
                "测试环境闭环任务计划完整",
                f"walkthrough_env=test 的计划缺少闭环任务: {', '.join(missing)}",
            )
    except Exception as exc:
        if task_type in {
            "code",
            "quality",
            "product-acceptance",
            "pr",
            "sprint-close",
            "promote-test",
            "build-image",
        }:
            result.blocked.append(f"harness.yml 加载失败: {exc}")
        else:
            result.warnings.append(f"harness.yml 加载失败: {exc}")
    if phase == "review" and task_type == "pr" and approval:
        approval_path = root / fill_pattern(approval, sprint_id)
        if approval_path.exists() and not approval_loaded:
            approval_record = load_mapping_evidence(approval_path, result, "审批记录")
        commit = str(approval_record.get("commit_sha", "")).strip()
        if not commit:
            result.blocked.append("Boss signoff 缺少 commit_sha")
        else:
            validate_signoff_delivery_refs(root, config, commit, result)
    if phase == "review" and task_type == "dependency-change":
        sessions = []
        state_root = root / ".harness/state/dependency-sessions"
        for path in sorted(state_root.glob("*.json")) if state_root.exists() else []:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if item.get("consumer_task_id") == (task_id or task_type):
                sessions.append((path, item))
        result.check(
            len(sessions) == 1,
            "dependency-change 唯一 session 已登记",
            f"dependency-change 需要且只能绑定一个 session，实际 {len(sessions)}",
        )
        if len(sessions) == 1:
            path, session = sessions[0]
            errors = validate_session(session)
            result.check(not errors, f"dependency session 完整: {path}", f"dependency session 无效: {errors}")
            result.check(
                session.get("status") == "completed",
                "dependency session 已完成",
                f"dependency session 尚未完成: {session.get('status')}",
            )
    if phase == "review" and task_type == "library-contract":
        session_id = header_field(content, "dependency_session")
        result.check(bool(session_id), "Library Sprint 已绑定 dependency session", "缺少 dependency_session 输入")
        if session_id:
            state_path = root / ".harness/state/dependency-sessions/incoming" / f"{session_id}.json"
            try:
                session = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                result.blocked.append(f"消费者契约状态不可读取: {state_path}: {exc}")
            else:
                errors = validate_session(session)
                result.check(not errors, "dependency session 摘要有效", f"dependency session 无效: {errors}")
                result.check(
                    session.get("provider_sprint") == sprint_id,
                    "dependency session Provider Sprint 匹配",
                    f"provider_sprint 不匹配: {session.get('provider_sprint')} != {sprint_id}",
                )
                result.check(
                    session.get("status") == "consumer-verified",
                    "消费者契约已通过",
                    f"消费者契约尚未通过: {session.get('status')}",
                )
    delivery_contract = (rules.get("sprint_delivery_contracts") or {}).get(sprint_type, {})
    terminal_tasks = set(delivery_contract.get("terminal_tasks", [])) if isinstance(delivery_contract, dict) else set()
    if phase == "review" and task_type in terminal_tasks:
        outcome_errors = validate_sprint_outcome_evidence(
            root,
            sprint_file,
            task_id or task_type,
            task_type,
            delivery_contract.get("outcome_producer"),
        )
        if outcome_errors:
            result.blocked.extend(outcome_errors)
        else:
            result.passed.append("Sprint outcome evidence 已绑定交付契约并覆盖全部验收条件")
    attempt_errors: list[str] = []
    if phase == "review":
        evidence_rules = rules_path or HarnessPaths.detect(project=root).rules / "task-rules.yml"
        attempt_errors = validate_attempt(root, sprint_file, evidence_rules, task_id or task_type, task_type, task)
        for error in attempt_errors:
            result.blocked.append(error)
    if run_commands and phase == "review" and not result.blocked and not attempt_errors and task.get("artifact_action"):
        execution = execute_action(
            task["artifact_action"],
            root=root,
            mode=load_harness_config()["project"]["mode"],
            phase="artifact",
            values={"sprint": sprint_id, "task_id": task_id or task_type},
        )
        record_phase(
            root,
            sprint_file,
            rules_path or HarnessPaths.detect(project=root).rules / "task-rules.yml",
            task_id or task_type,
            task_type,
            task,
            "artifact",
            task["artifact_action"],
            {"sprint": sprint_id},
            execution.returncode,
        )
        result.check(
            execution.ok,
            f"产出物 action 通过: {task['artifact_action']}",
            f"产出物 action 未通过: {task['artifact_action']} {execution.stderr}",
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_type")
    parser.add_argument("sprint_plan_file", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--phase", choices=("preflight", "review"), default="preflight")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--max-retry", type=int, help="临时覆盖 harness.yml#task_execution.max_review_retries")
    retry_action = parser.add_mutually_exclusive_group()
    retry_action.add_argument("--increment-retry", action="store_true")
    retry_action.add_argument("--reset-retry", action="store_true")
    parser.add_argument("--reset-retry-reason")
    parser.add_argument("--new-attempt", action="store_true", help="Review FAIL 后开始全新的 Plan/Exec/Review 轮次")
    parser.add_argument("--preflight-ttl", type=int)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    paths = HarnessPaths.detect(project=root)
    rules_file = paths.rules / "task-rules.yml"
    if not rules_file.exists() or not args.sprint_plan_file.exists():
        print("BLOCKED — task-rules.yml 或 Sprint 计划不存在")
        return 1
    rules = load_yaml(rules_file)
    schema = validate_rules(rules, root)
    if schema.errors:
        for message in schema.errors:
            print(f"❌ {message}")
        return 1
    ttl = args.preflight_ttl or int((rules.get("sprint_preflight") or {}).get("ttl_seconds", 600))
    result = GateResult()
    harness_config = load_harness_config()
    max_retry = (
        args.max_retry if args.max_retry is not None else int(harness_config["task_execution"]["max_review_retries"])
    )
    if not 0 <= max_retry <= 5:
        parser.error("Review 重试上限必须是 0 到 5 的整数")
    if args.reset_retry and not args.reset_retry_reason:
        parser.error("--reset-retry 必须同时提供 --reset-retry-reason")
    if args.new_attempt and not args.increment_retry:
        parser.error("--new-attempt 必须与 --increment-retry 同时使用")
    if args.phase != "preflight" and (args.increment_retry or args.reset_retry or args.new_attempt):
        parser.error("重试状态只能在 preflight 阶段变更")
    result.blocked.extend(
        validate_sprint_context(
            root,
            args.sprint_plan_file.resolve(),
            rules,
            allow_completed=allows_completed_plan(args.task_type, args.phase),
        )
    )
    result.blocked.extend(validate_sprint_activation(root, args.sprint_plan_file.resolve()))
    sprint_rows = table_rows(args.sprint_plan_file.read_text(encoding="utf-8"))
    task_row = next((row for row in sprint_rows if row.get("id") == args.task_id), {})
    row_type = task_row.get("类型") or task_row.get("type")
    if not task_row:
        result.blocked.append(f"任务 ID 未登记在 Sprint 计划中: {args.task_id}")
    elif row_type != args.task_type:
        result.blocked.append(f"任务 ID/类型不匹配: {args.task_id}={row_type}, requested={args.task_type}")
    if result.blocked:
        for message in result.blocked:
            print(f"❌ {message}")
        print("BLOCKED")
        return 1
    origin = task_row.get("来源") or task_row.get("origin")
    parent = task_row.get("父任务") or task_row.get("parent")
    retry_budget_id = parent if origin == "remediation" and parent else args.task_id
    retry_gate(
        root,
        args.sprint_plan_file.stem,
        retry_budget_id or args.task_type,
        max_retry,
        "reset" if args.reset_retry else "increment" if args.increment_retry else None,
        result,
        args.reset_retry_reason or "",
    )
    if result.blocked:
        for message in result.blocked:
            print(f"❌ {message}")
        print("BLOCKED")
        return 1
    preflight_id = (rules.get("sprint_preflight") or {}).get("action", "")
    mode = harness_config["project"]["mode"]
    task = (rules.get("tasks") or {}).get(args.task_type, {})
    pending_attempt = None
    if args.phase == "preflight":
        # Revoke any previous executable attempt before running checks that may
        # fail, crash, or create side effects.
        pending_attempt = ensure_attempt(
            root,
            args.sprint_plan_file.resolve(),
            rules_file,
            args.task_id,
            args.task_type,
            task,
            new_attempt=args.new_attempt,
        )
    if args.phase == "preflight" and preflight_id:
        command = [
            *action_argv(preflight_id, root=root, mode=mode, phase="preflight"),
            "--skip-if-recent",
            str(ttl),
        ]
        execution = try_run(command, cwd=root)
        result.check(execution.ok, f"Preflight action 通过: {preflight_id}", f"Preflight action 未通过: {preflight_id}")
    elif args.phase == "preflight":
        result.blocked.append("sprint_preflight.action 未配置")
    evaluated = evaluate(
        args.task_type,
        args.sprint_plan_file.resolve(),
        rules,
        root,
        strict=args.strict,
        phase=args.phase,
        rules_path=rules_file,
        task_id=args.task_id,
    )
    result.passed += evaluated.passed
    result.blocked += evaluated.blocked
    result.warnings += evaluated.warnings
    infra = None
    if args.phase == "preflight" and (action_id := task.get("infra_action")):
        infra = action_argv(
            action_id,
            root=root,
            mode=mode,
            phase="preflight",
            values={
                "sprint": args.sprint_plan_file.stem,
                "task_id": args.task_id,
                "run_id": str((pending_attempt or {}).get("run_id", "")),
            },
        )
    if infra and not result.blocked:
        if task.get("infra_cache", "ttl") == "never":
            execution = try_run(infra, cwd=root, timeout=resolve_action(action_id).timeout_seconds)
            success, detail = execution.ok, (execution.stdout or execution.stderr).strip()[-2000:]
        else:
            success, detail = run_cached_command(
                infra,
                root / ".harness/infra-ready-cache.json",
                infra_cache_key(root, args.task_type, action_id, infra),
                ttl,
                root,
                resolve_action(action_id).timeout_seconds,
            )
        result.check(success, f"infra_action 通过: {action_id}", f"infra_action 未通过: {action_id} {detail}")
    if args.phase == "preflight" and not result.blocked:
        activate_attempt(
            root,
            args.sprint_plan_file.resolve(),
            rules_file,
            args.task_id,
            args.task_type,
            task,
        )
    for message in result.passed:
        print(f"✅ {message}")
    for message in result.warnings:
        print(f"⚠️  {message}")
    for message in result.blocked:
        print(f"❌ {message}")
    success = (
        f"PASS — {args.task_type} 任务产出已通过闭环检查"
        if args.phase == "review"
        else f"PASS — 可以开始 {args.task_type} 任务"
    )
    print("BLOCKED" if result.blocked else success)
    return 1 if result.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())

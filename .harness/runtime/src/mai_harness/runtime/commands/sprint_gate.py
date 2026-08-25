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

from mai_harness.runtime.application.action_executor import action_argv, execute_action
from mai_harness.runtime.application.dependency_session import validate_session
from mai_harness.runtime.application.sprint_context import (
    validate_sprint_activation,
    validate_sprint_context,
)
from mai_harness.runtime.application.task_evidence import (
    activate_attempt,
    ensure_attempt,
    record_phase,
    validate_attempt,
    validate_failed_action_evidence,
)
from mai_harness.runtime.commands.validate_task_rules import validate as validate_rules
from mai_harness.runtime.domain.actions import resolve_action
from mai_harness.runtime.domain.modes import PROJECT_TYPES
from mai_harness.runtime.domain.sprint_context import header_field, table_rows
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run

DONE = re.compile(r"^(done|完成|通过)$", re.I)
ROLLBACK = re.compile(r"^(rollback|回退)$", re.I)


@dataclass
class GateResult:
    passed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def check(self, condition: bool, success: str, failure: str) -> None:
        (self.passed if condition else self.blocked).append(success if condition else failure)


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
    statuses: dict[str, list[str]] = {}
    for task_id, task_type, status in parse_task_rows(content):
        statuses.setdefault(task_type, []).append(status)
        if task_id:
            statuses.setdefault(task_id, []).append(status)
    return statuses


def task_keyword(text: str, task_names: list[str]) -> str:
    value = str(text)
    known = next((key for key in task_names if key in value), "")
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


def sprint_list_field(content: str, field: str) -> list[str]:
    """Read a compact YAML-style list from the Sprint header without treating Markdown as YAML."""
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*\[([^]]*)]\s*$", content)
    return [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()] if match else []


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
        matching_rows = [row for row in task_rows if row[1] == task_type and (not task_id or row[0] == task_id)]
        if len(matching_rows) != 1:
            result.blocked.append(f"当前任务未登记到 Sprint 任务表: {task_id or task_type} ({task_type})")
            return result
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
            if not present and not (isinstance(earlier_stage, dict) and earlier_stage.get("optional") is True):
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
            if requirement == "all" and set(present) != set(applicable):
                missing = [name for name in applicable if name not in present]
                result.blocked.append(f"前序阶段任务未列入 Sprint(all): {', '.join(missing)}")
                continue
            satisfied = bool(completed) if requirement == "any" else len(completed) == len(present)
            if present and not satisfied:
                result.blocked.append(f"前序阶段未完成({requirement}): {', '.join(present)}")
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
    outcome_satisfied = outcome.get("task") and all(
        status.lower() in outcome_statuses for status in statuses.get(outcome.get("task"), [])
    )
    if outcome.get("require_action_evidence") is True and outcome_satisfied:
        source_rows = [row for row in task_rows if row[1] == outcome.get("task")]
        for source_id, source_type, _ in source_rows:
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
    if external.get("kind") == "sprint-signoffs":
        source_sprints = sprint_list_field(content, external.get("field", "source_sprints"))
        result.check(bool(source_sprints), "已声明 source_sprints", "Sprint 计划缺少非空 source_sprints: [...] 输入")
        result.check(
            len(source_sprints) == len(set(source_sprints)),
            "source_sprints 无重复",
            "source_sprints 含重复 Sprint",
        )
        for source_sprint in source_sprints:
            if not re.fullmatch(r"sprint-\d+-[a-z0-9][a-z0-9-]*", source_sprint):
                result.blocked.append(f"source_sprints 含非法 Sprint ID: {source_sprint}")
                continue
            signoff = root / "docs/acceptance-reports" / f"{source_sprint}-boss-signoff.yml"
            loaded = load_yaml(signoff) if signoff.exists() else {}
            record = loaded if isinstance(loaded, dict) else {}
            if signoff.exists() and not isinstance(loaded, dict):
                result.blocked.append(f"源 Sprint 审批格式非法（顶层必须是对象）: {signoff}")
            commit = str(record.get("commit_sha", ""))
            result.check(signoff.exists(), f"源 Sprint 审批存在: {source_sprint}", f"源 Sprint 审批缺失: {signoff}")
            result.check(
                record.get("decision") == "approved",
                f"源 Sprint 已批准: {source_sprint}",
                f"源 Sprint 未批准: {source_sprint}",
            )
            result.check(
                record.get("sprint") == source_sprint,
                f"源 Sprint 审批身份匹配: {source_sprint}",
                f"源 Sprint 审批身份不匹配: {record.get('sprint', '缺失')} != {source_sprint}",
            )
            commit_exists = (
                bool(re.fullmatch(r"[a-f0-9]{7,40}", commit))
                and try_run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=root).ok
            )
            result.check(
                commit_exists, f"源 Sprint commit 有效: {commit}", f"源 Sprint commit 无效: {commit or '缺失'}"
            )
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
    if approval:
        path = root / fill_pattern(approval, sprint_id)
        result.check(path.exists(), f"审批记录存在: {path}", f"审批记录不存在: {path}")
        if path.exists():
            result.check(load_yaml(path).get("decision") == "approved", "审批记录状态为 approved", "审批记录未放行")
    for declaration in task.get("preflight_file_checks", []):
        relative = re.sub(r"\s*必须存在.*$", "", declaration).strip()
        path = root / fill_pattern(relative, sprint_id)
        result.check(path.exists(), f"产出物存在: {relative}", f"产出物缺失: {relative}")
    state_key = {"build-image": "ready", "promote-test": "success"}.get(task_type)
    if state_key:
        environment_match = re.search(r"(test|prod|staging)", sprint_id)
        environment = environment_match.group(1) if environment_match else "test"
        if task_type == "build-image":
            state_path = root / f".harness/state/promote-prep-{environment}.json"
        else:
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
        if task_type == "code" and config.get("gates", {}).get("ui_design_l3") is True:
            approval_path = root / "docs/design-docs" / f"{series}-design-approval.yml"
            result.check(
                approval_path.exists() and load_yaml(approval_path).get("decision") == "approved",
                "UI Design L3 审批通过",
                f"UI Design L3 审批缺失或未放行: {approval_path}",
            )
        if (
            task_type in {"quality", "product-acceptance", "pr", "sprint-close"}
            and config.get("walkthrough_env") == "test"
            and "code" in statuses
        ):
            required = (
                "promote-prep",
                "build-image",
                "promote-test",
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
            "promote-prep",
            "promote-test",
            "build-image",
        }:
            result.blocked.append(f"harness.yml 加载失败: {exc}")
        else:
            result.warnings.append(f"harness.yml 加载失败: {exc}")
    if phase == "review" and task_type == "pr" and approval:
        approval_path = root / fill_pattern(approval, sprint_id)
        commit = str(load_yaml(approval_path).get("commit_sha", "")).strip() if approval_path.exists() else ""
        if not commit:
            result.blocked.append("Boss signoff 缺少 commit_sha")
        else:
            refs = ["origin/develop", "origin/test"] if config.get("walkthrough_env") == "test" else ["origin/develop"]
            fetch = try_run(
                ["git", "fetch", "origin", *[ref.removeprefix("origin/") for ref in refs], "--quiet"], cwd=root
            )
            result.check(fetch.ok, "目标分支引用已刷新", "无法刷新远端目标分支引用")
            if fetch.ok:
                for ref in refs:
                    contained = try_run(["git", "merge-base", "--is-ancestor", commit, ref], cwd=root)
                    result.check(
                        contained.ok, f"signoff commit_sha 已抵达 {ref}", f"signoff commit_sha 未抵达 {ref}: {commit}"
                    )
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
            values={"sprint": sprint_id},
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
            allow_completed=args.task_type == "pr",
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
    if args.phase == "preflight":
        # Revoke any previous executable attempt before running checks that may
        # fail, crash, or create side effects.
        ensure_attempt(
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
    infra = None
    if args.phase == "preflight" and (action_id := task.get("infra_action")):
        infra = action_argv(
            action_id,
            root=root,
            mode=mode,
            phase="preflight",
            values={"sprint": args.sprint_plan_file.stem},
        )
    if infra and not result.blocked:
        success, detail = run_cached_command(
            infra,
            root / ".harness/infra-ready-cache.json",
            infra_cache_key(root, args.task_type, action_id, infra),
            ttl,
            root,
            resolve_action(action_id).timeout_seconds,
        )
        result.check(success, f"infra_action 通过: {action_id}", f"infra_action 未通过: {action_id} {detail}")
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

#!/usr/bin/env python3
"""Schema validation for lint/task-rules.yml."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mai_harness.runtime.domain.actions import resolve_action
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import load_yaml

KNOWN_GATES = {"L1", "L3"}
KNOWN_STACKS = {"python-backend", "fullstack", "frontend"}
KNOWN_TASKS = {
    "infra",
    "product",
    "design",
    "backend-design",
    "frontend-design",
    "code",
    "test-case-gen",
    "quality",
    "pr",
    "product-acceptance",
    "sprint-close",
    "observe",
    "promote-prep",
    "build-image",
    "promote-test",
    "integration",
    "release-prep",
    "migration-design",
    "regression",
    "release-approval",
    "prod-deploy",
    "hotfix-init",
    "back-merge",
    "managed-project-check",
    "assignment-dispatch",
    "assignment-status",
    "delivery-verify",
    "release-compose",
    "test-integration",
    "test-deploy",
    "integration-finding",
    "release-promote",
    "release-rollback",
}


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, list):
        return [item for index, child in enumerate(value) for item in strings(child, f"{path}[{index}]")]
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in strings(child, f"{path}.{key}" if path else key)]
    return []


def validate_acceptance(items: Any, path: str, error, warning) -> list[str]:
    if not isinstance(items, list) or not items:
        error(f"{path} 必须是非空数组")
        return []
    identifiers: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, str) and item:
            identifiers.append(item if re.fullmatch(r"[a-z0-9][a-z0-9.-]*", item) else f"legacy:{item}")
        elif isinstance(item, dict) and set(item) == {"id", "text"}:
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", str(item.get("id", ""))):
                error(f"{path}[{index}].id 非法")
            if not isinstance(item.get("text"), str) or not item["text"]:
                error(f"{path}[{index}].text 必须是非空字符串")
            identifiers.append(str(item.get("id", "")))
        else:
            error(f"{path}[{index}] 必须是字符串或仅含 id/text 的对象")
    if len(identifiers) != len(set(identifiers)):
        error(f"{path} acceptance ID 重复")
    return identifiers


def validate(doc: Any, root: Path) -> Validation:
    result = Validation()
    error, warning = result.errors.append, result.warnings.append
    if not isinstance(doc, dict):
        error("task-rules.yml 顶层必须是对象")
        return result
    if not doc.get("version"):
        warning("缺少 version 字段")
    preflight = doc.get("sprint_preflight")
    if preflight and (not isinstance(preflight.get("ttl_seconds"), (int, float)) or preflight["ttl_seconds"] <= 0):
        error("sprint_preflight.ttl_seconds 必须是正数")
    if preflight:
        try:
            resolve_action(preflight.get("action", ""))
        except ValueError:
            error(f"sprint_preflight.action 未在 Python Action Registry 注册: {preflight.get('action', '')}")
    tasks = doc.get("tasks") or {}
    if not tasks:
        error("tasks 为空")
    mode_matrix = doc.get("mode_task_capabilities")
    if not isinstance(mode_matrix, dict) or set(mode_matrix) != {"standalone", "managed", "control"}:
        error("mode_task_capabilities 必须完整声明三种模式")
        mode_matrix = {}
    for mode, names in mode_matrix.items():
        if not isinstance(names, list) or len(names) != len(set(names)):
            error(f"mode_task_capabilities.{mode} 必须是无重复任务数组")
        elif unknown := set(names) - set(tasks):
            error(f"mode_task_capabilities.{mode} 含未知任务: {sorted(unknown)}")
    if missing_capabilities := set(tasks) - {name for names in mode_matrix.values() for name in names}:
        error(f"以下任务未登记任何模式（默认拒绝）: {sorted(missing_capabilities)}")
    sprint_matrix = doc.get("sprint_type_task_capabilities")
    expected_sprint_types = {
        "feature-sprint",
        "deploy-sprint-test",
        "deploy-sprint-prod",
        "hotfix",
        "control",
        "maintenance",
    }
    if not isinstance(sprint_matrix, dict) or set(sprint_matrix) != expected_sprint_types:
        error(f"sprint_type_task_capabilities 必须完整声明: {sorted(expected_sprint_types)}")
        sprint_matrix = {}
    for sprint_type, names in sprint_matrix.items():
        if not isinstance(names, list) or len(names) != len(set(names)):
            error(f"sprint_type_task_capabilities.{sprint_type} 必须是无重复任务数组")
        elif unknown := set(names) - set(tasks):
            error(f"sprint_type_task_capabilities.{sprint_type} 含未知任务: {sorted(unknown)}")
    if missing_sprint_types := set(tasks) - {name for names in sprint_matrix.values() for name in names}:
        error(f"以下任务未登记任何 sprint_type（默认拒绝）: {sorted(missing_sprint_types)}")
    sequences = doc.get("sprint_type_sequences")
    if not isinstance(sequences, dict) or set(sequences) != expected_sprint_types:
        error(f"sprint_type_sequences 必须完整声明: {sorted(expected_sprint_types)}")
        sequences = {}
    for sprint_type, stages in sequences.items():
        stage_tasks = (
            [stage.get("tasks", []) if isinstance(stage, dict) else [] for stage in stages]
            if isinstance(stages, list)
            else []
        )
        flattened = [name for stage in stage_tasks for name in stage] if isinstance(stages, list) else []
        valid_names = all(isinstance(name, str) and name for name in flattened)
        if (
            not isinstance(stages, list)
            or any(
                not isinstance(stage, dict) or not isinstance(stage.get("tasks"), list) or not stage["tasks"]
                for stage in stages
            )
            or any(stage.get("require", "all") not in {"all", "any"} for stage in stages)
            or not valid_names
            or (valid_names and len(flattened) != len(set(flattened)))
            or (valid_names and set(flattened) != set(sprint_matrix.get(sprint_type, [])))
        ):
            error(
                f"sprint_type_sequences.{sprint_type} 必须完整覆盖能力矩阵，且阶段含 tasks、require=all|any、任务不重复"
            )
    type_mode_matrix = doc.get("sprint_type_mode_capabilities")
    if not isinstance(type_mode_matrix, dict) or set(type_mode_matrix) != {"standalone", "managed", "control"}:
        error("sprint_type_mode_capabilities 必须完整声明三种模式")
    else:
        for mode, sprint_types in type_mode_matrix.items():
            unknown = set(sprint_types or []) - expected_sprint_types if isinstance(sprint_types, list) else set()
            if not isinstance(sprint_types, list) or unknown:
                error(f"sprint_type_mode_capabilities.{mode} 含非法类型: {sorted(unknown)}")
    policies = doc.get("sprint_type_policies")
    if not isinstance(policies, dict) or set(policies) != expected_sprint_types:
        error(f"sprint_type_policies 必须完整声明: {sorted(expected_sprint_types)}")
    else:
        for sprint_type, policy in policies.items():
            if not isinstance(policy, dict) or set(policy) != {"worktree", "base", "branch_prefix"}:
                error(f"sprint_type_policies.{sprint_type} 必须声明 worktree/base/branch_prefix")
                continue
            if policy.get("worktree") not in {"required", "forbidden"}:
                error(f"sprint_type_policies.{sprint_type}.worktree 非法")
            if policy.get("base") not in {"development", "test", "production"}:
                error(f"sprint_type_policies.{sprint_type}.base 非法")
            prefix = policy.get("branch_prefix")
            if not isinstance(prefix, str) or (policy.get("worktree") == "required" and not prefix.endswith("/")):
                error(f"sprint_type_policies.{sprint_type}.branch_prefix 非法")
    layout_path = (
        root / "config/distribution-layout.yml"
        if (root / "config/distribution-layout.yml").exists()
        else root / ".harness/distribution-layout.yml"
    )
    layout = load_yaml(layout_path) if layout_path.exists() else {}
    generated = layout.get("generated", {}) if isinstance(layout, dict) else {}
    if generated:
        test_case_outputs = (tasks.get("test-case-gen") or {}).get("outputs", {})
        if test_case_outputs.get("path", "").rstrip("/") != generated.get("test_cases"):
            error("tasks.test-case-gen.outputs.path 与 distribution-layout.generated.test_cases 不一致")
        code_output = str((tasks.get("code") or {}).get("outputs", {}).get("path", ""))
        if generated.get("e2e") not in code_output:
            error("tasks.code.outputs.path 未包含 distribution-layout.generated.e2e")
    sections = doc.get("doc_section_rules")
    if sections is not None and not isinstance(sections, dict):
        error("doc_section_rules 必须是对象")
    elif sections:
        for directory, patterns in sections.items():
            if not isinstance(patterns, list):
                error(f'doc_section_rules["{directory}"] 必须是数组')
                continue
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except (re.error, TypeError) as exc:
                    error(f'doc_section_rules["{directory}"] 含非法正则: {pattern} ({exc})')
    keywords = sorted(KNOWN_TASKS, key=len, reverse=True)
    for name, task in tasks.items():
        if name not in KNOWN_TASKS:
            warning(f"未知任务类型: {name}")
        if not isinstance(task, dict):
            error(f"tasks.{name} 必须是对象")
            continue
        for path, value in strings(task, f"tasks.{name}"):
            if re.search(r"sprint-N-name|sprint-N(?:\b|-)", value):
                error(f"{path} 使用 legacy sprint token: {value}")
        for required in ("label", "gate", "tools", "acceptance"):
            if required not in task:
                error(f"tasks.{name} 缺少必填字段: {required}")
        if task.get("gate") and task["gate"] not in KNOWN_GATES:
            error(f"tasks.{name}.gate 非法: {task['gate']}")
        for key in ("specs", "specs-frontend", "specs-backend"):
            if key in task and (
                not isinstance(task[key], list) or not all(isinstance(item, str) for item in task[key])
            ):
                error(f"tasks.{name}.{key} 必须是字符串数组")
        if "output" in task:
            error(f"tasks.{name}.output 已废弃，必须使用 outputs")
        outputs = task.get("outputs")
        if outputs is not None and not isinstance(outputs, dict):
            error(f"tasks.{name}.outputs 必须是对象")
            outputs = {}
        if isinstance(outputs, dict):
            output_path = outputs.get("path")
            if output_path is not None and not isinstance(output_path, str):
                error(f"tasks.{name}.outputs.path 必须是字符串")
            for value in (output_path, outputs.get("index")):
                if not isinstance(value, str):
                    continue
                normalized = value.lstrip("./")
                if normalized.startswith(("test-cases/", "e2e/", "test-reports/")):
                    error(f"tasks.{name}.outputs 使用旧分发布局: {value}")
            index = outputs.get("index")
            if isinstance(output_path, str) and isinstance(index, str):
                roots = [item.strip().rstrip("/") for item in output_path.split(" 或 ")]
                if "<" not in output_path and not any(index == root or index.startswith(root + "/") for root in roots):
                    error(f"tasks.{name}.outputs.index 不在 outputs.path 内: {index}")
        allowed_stacks = task.get("allowed_stacks")
        if allowed_stacks is not None and (
            not isinstance(allowed_stacks, list) or not allowed_stacks or not set(allowed_stacks) <= KNOWN_STACKS
        ):
            error(f"tasks.{name}.allowed_stacks 必须是已知 stack 的非空数组")
        declared_modes = task.get("allowed_modes")
        derived_modes = {mode for mode, names in mode_matrix.items() if name in names}
        if declared_modes is not None and set(declared_modes) != derived_modes:
            error(f"tasks.{name}.allowed_modes 与 mode_task_capabilities 不一致")
        common_acceptance = validate_acceptance(task.get("acceptance"), f"tasks.{name}.acceptance", error, warning)
        if task.get("execution_protocol") not in {None, "agent", "action", "orchestrator"}:
            error(f"tasks.{name}.execution_protocol 非法")
        if task.get("review_protocol") not in {None, "agent-full", "artifact-only"}:
            error(f"tasks.{name}.review_protocol 非法")
        acceptance_by_stack = task.get("acceptance_by_stack")
        if acceptance_by_stack is not None:
            if not isinstance(acceptance_by_stack, dict) or not set(acceptance_by_stack) <= KNOWN_STACKS:
                error(f"tasks.{name}.acceptance_by_stack 只能声明已知 stack")
            else:
                for stack, items in acceptance_by_stack.items():
                    stack_ids = validate_acceptance(items, f"tasks.{name}.acceptance_by_stack.{stack}", error, warning)
                    if set(common_acceptance) & set(stack_ids):
                        error(f"tasks.{name}.acceptance 与 {stack} acceptance ID 重复")
        tools = task.get("tools")
        if isinstance(tools, dict):
            for key in ("allow", "deny"):
                if not isinstance(tools.get(key), list):
                    error(f"tasks.{name}.tools.{key} 必须是数组")
        for legacy in ("infra_ready", "entry_command", "artifact_guard"):
            if legacy in task:
                error(f"tasks.{name}.{legacy} 已废弃，必须使用 Action Registry 字段")
        execute_parameters = (task.get("execute") or {}).get("parameters", [])
        if not isinstance(execute_parameters, list) or not all(
            isinstance(item, str) and item for item in execute_parameters
        ):
            error(f"tasks.{name}.execute.parameters 必须是非空字符串数组")
            execute_parameters = []
        for action_path, action_id in (
            (f"tasks.{name}.infra_action", task.get("infra_action")),
            (f"tasks.{name}.execute.action", (task.get("execute") or {}).get("action")),
            (f"tasks.{name}.entry_action", task.get("entry_action")),
            (f"tasks.{name}.artifact_action", task.get("artifact_action")),
        ):
            if action_id:
                try:
                    action = resolve_action(action_id)
                    phase = (
                        "preflight"
                        if action_path.endswith("infra_action")
                        else "execute"
                        if action_path.endswith("execute.action")
                        else "entry"
                        if action_path.endswith("entry_action")
                        else "artifact"
                    )
                    if phase not in action.phases:
                        error(f"{action_path} 不允许用于 {phase} 阶段")
                    task_modes = set(task.get("allowed_modes") or {"standalone", "managed", "control"})
                    if not task_modes <= action.modes:
                        error(f"{action_path} 不允许用于模式: {sorted(task_modes - action.modes)}")
                    if action_path.endswith("execute.action"):
                        declared = set(execute_parameters)
                        expected = set(action.parameters) - {"sprint"}
                        if declared != expected:
                            error(f"tasks.{name}.execute.parameters 必须与 Action 参数一致: {sorted(expected)}")
                except ValueError:
                    error(f"{action_path} 未在 Python Action Registry 注册: {action_id}")
        if task.get("allowed_modes") == ["control"] and not outputs:
            error(f"tasks.{name}: Control 任务必须声明可验证 outputs")
        for key in ("completion_checks", "prerequisites"):
            if key in task and not isinstance(task[key], list):
                error(f"tasks.{name}.{key} 必须是数组")
        for prerequisite in task.get("prerequisites", []):
            keyword = next((item for item in keywords if item in str(prerequisite)), "")
            if keyword and keyword not in tasks:
                error(f"tasks.{name}.prerequisites 引用未定义任务: {keyword}")
            elif keyword:
                source_stacks = set((tasks.get(keyword) or {}).get("allowed_stacks") or KNOWN_STACKS)
                target_stacks = set(task.get("allowed_stacks") or KNOWN_STACKS)
                if not source_stacks & target_stacks:
                    error(f"tasks.{name}.prerequisites 与 {keyword} 无可共同执行的 stack")
        for group in task.get("prerequisites_any", []):
            if not isinstance(group, list) or not group or not all(item in tasks for item in group):
                error(f"tasks.{name}.prerequisites_any 必须引用已定义任务")
                continue
            target_stacks = set(task.get("allowed_stacks") or KNOWN_STACKS)
            for stack in target_stacks:
                if not any(stack in set((tasks[item] or {}).get("allowed_stacks") or KNOWN_STACKS) for item in group):
                    error(f"tasks.{name}.prerequisites_any 在 stack={stack} 无可达前置")
        external = task.get("external_evidence")
        if external is not None and (
            not isinstance(external, dict)
            or external.get("kind") != "sprint-signoffs"
            or not isinstance(external.get("field"), str)
        ):
            error(f"tasks.{name}.external_evidence 必须声明 kind=sprint-signoffs 和 field")
        outcome = task.get("upstream_outcome")
        if outcome is not None and (
            not isinstance(outcome, dict)
            or outcome.get("task") not in tasks
            or not isinstance(outcome.get("statuses"), list)
            or not outcome["statuses"]
            or ("require_action_evidence" in outcome and type(outcome["require_action_evidence"]) is not bool)
        ):
            error(f"tasks.{name}.upstream_outcome 必须引用任务并声明非空 statuses")
        for key in ("completion_checks",):
            values = task.get(key, [])
            values = values if isinstance(values, list) else [values]
            for command in values:
                if not isinstance(command, str):
                    continue
                for script in re.findall(r"\bscripts/([A-Za-z0-9._-]+\.(?:py|mjs))\b", command):
                    error(f"tasks.{name}.{key} 仍引用旧脚本 scripts/{script}；必须改用 harness <command> 或 Action ID")
    for gate in KNOWN_GATES:
        if gate not in (doc.get("gates") or {}):
            warning(f"gates.{gate} 未定义")
    for index, rule in enumerate(doc.get("spawn_rules") or []):
        if rule.get("require", "all") not in {"all", "any"}:
            error(f"spawn_rules[{index}].require 必须为 all/any")
        for side in ("from", "to"):
            values = rule.get(side, [])
            values = values if isinstance(values, list) else [values]
            for value in values:
                if value not in tasks:
                    error(f"spawn_rules[{index}].{side} 引用未定义任务: {value}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=PATHS.rules / "task-rules.yml")
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()
    if not args.file.exists():
        parser.error(f"task-rules.yml 不存在: {args.file}")
    try:
        result = validate(load_yaml(args.file), Path.cwd())
    except Exception as exc:
        print(f"FAIL YAML 解析失败: {exc}")
        return 1
    for message in result.errors:
        print(f"❌ {message}")
    for message in result.warnings:
        print(f"⚠️  {message}")
    print(f"{'FAIL' if result.errors else 'PASS'}: {len(result.errors)} 错误, {len(result.warnings)} 警告")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

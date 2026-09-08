#!/usr/bin/env python3
"""Schema validation for lint/task-rules.yml."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.migration_progress import policy_errors
from mai_harness.runtime.domain.actions import resolve_action
from mai_harness.runtime.domain.document_registry import REGISTRY_TASKS
from mai_harness.runtime.domain.modes import PROJECT_TYPES
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.utils import load_yaml

KNOWN_GATES = {"L1", "L3"}


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
    result.errors.extend(policy_errors(doc.get("migration_execution_policy")))
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
    input_projections = doc.get("task_input_projections", {})
    if not isinstance(input_projections, dict):
        error("task_input_projections 必须是对象")
        input_projections = {}
    for consumer, producers in input_projections.items():
        path = f"task_input_projections.{consumer}"
        if consumer not in tasks:
            error(f"{path} 消费者任务不存在")
        if (
            not isinstance(producers, list)
            or not producers
            or not all(isinstance(item, str) and item for item in producers)
            or len(producers) != len(set(producers))
        ):
            error(f"{path} 必须是无重复的非空任务类型数组")
            continue
        if unknown := set(producers) - set(tasks):
            error(f"{path} 含未知生产者任务: {sorted(unknown)}")
        if consumer in producers:
            error(f"{path} 不得投影自身")
    impact_requirements = doc.get("impact_surface_requirements")
    if not isinstance(impact_requirements, dict) or not impact_requirements:
        error("impact_surface_requirements 必须是非空对象")
        impact_requirements = {}
    for surface, definition in impact_requirements.items():
        if not isinstance(surface, str) or not surface or not isinstance(definition, dict):
            error(f"impact_surface_requirements.{surface} 格式非法")
            continue
        if set(definition) != {"tasks", "facets"}:
            error(f"impact_surface_requirements.{surface} 必须仅包含 tasks/facets")
            continue
        required_tasks = definition.get("tasks")
        facet_map = definition.get("facets")
        if (
            not isinstance(required_tasks, list)
            or len(required_tasks) != len(set(required_tasks))
            or not set(required_tasks) <= {"design", "backend-design", "frontend-design"}
        ):
            error(f"impact_surface_requirements.{surface}.tasks 必须是无重复的设计任务数组")
            required_tasks = []
        if not isinstance(facet_map, dict) or not set(facet_map) <= set(required_tasks):
            error(f"impact_surface_requirements.{surface}.facets 只能引用本影响面的 tasks")
            continue
        for task_name, selected in facet_map.items():
            supported = (tasks.get(task_name) or {}).get("facets") or []
            if not isinstance(selected, list) or not selected or not set(selected) <= set(supported):
                error(f"impact_surface_requirements.{surface}.facets.{task_name} 必须引用任务已声明 facets")
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
        "library-sprint",
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
    requirement_modes = doc.get("sprint_requirement_modes")
    if not isinstance(requirement_modes, dict) or set(requirement_modes) != expected_sprint_types:
        error(f"sprint_requirement_modes 必须完整声明: {sorted(expected_sprint_types)}")
    else:
        allowed_requirement_modes = {"stories", "non-product-change"}
        for sprint_type, modes in requirement_modes.items():
            if (
                not isinstance(modes, list)
                or not modes
                or len(modes) != len(set(modes))
                or not set(modes) <= allowed_requirement_modes
            ):
                error(f"sprint_requirement_modes.{sprint_type} 必须是合法且无重复的非空数组")
        if requirement_modes.get("feature-sprint") != ["stories"]:
            error("feature-sprint 必须且只能使用 stories")
        if requirement_modes.get("control") != ["stories"]:
            error("control 必须且只能使用 stories")
    delivery_strategies = doc.get("delivery_strategies")
    if not isinstance(delivery_strategies, dict) or not delivery_strategies:
        error("delivery_strategies 必须是非空对象")
        delivery_strategies = {}
    for name, definition in delivery_strategies.items():
        if not isinstance(name, str) or not isinstance(definition, dict):
            error(f"delivery_strategies.{name} 必须是对象")
            continue
        outcomes = definition.get("observable_outcomes")
        required = definition.get("required_outcomes", [])
        if (
            not isinstance(outcomes, list)
            or not outcomes
            or not all(isinstance(item, str) and item for item in outcomes)
        ):
            error(f"delivery_strategies.{name}.observable_outcomes 必须是非空字符串数组")
            continue
        if not isinstance(required, list) or not set(required) <= set(outcomes):
            error(f"delivery_strategies.{name}.required_outcomes 必须是 observable_outcomes 子集")
    delivery_contracts = doc.get("sprint_delivery_contracts")
    if not isinstance(delivery_contracts, dict) or set(delivery_contracts) != expected_sprint_types:
        error(f"sprint_delivery_contracts 必须完整声明: {sorted(expected_sprint_types)}")
        delivery_contracts = {}
    for sprint_type, contract in delivery_contracts.items():
        if not isinstance(contract, dict) or set(contract) != {
            "strategies",
            "terminal_tasks",
            "outcome_producer",
        }:
            error(f"sprint_delivery_contracts.{sprint_type} 必须仅包含 strategies/terminal_tasks/outcome_producer")
            continue
        strategies = contract.get("strategies")
        terminal = contract.get("terminal_tasks")
        if not isinstance(strategies, list) or not strategies or set(strategies) - set(delivery_strategies):
            error(f"sprint_delivery_contracts.{sprint_type}.strategies 引用了未知策略")
        if (
            not isinstance(terminal, list)
            or (sprint_matrix.get(sprint_type) and not terminal)
            or set(terminal) - set(sprint_matrix.get(sprint_type, []))
        ):
            error(f"sprint_delivery_contracts.{sprint_type}.terminal_tasks 必须引用当前 Sprint 任务")
        if contract.get("outcome_producer") not in {"boss-signoff", "task-review"}:
            error(f"sprint_delivery_contracts.{sprint_type}.outcome_producer 非法")
    sequences = doc.get("sprint_type_sequences")
    if not isinstance(sequences, dict) or set(sequences) != expected_sprint_types:
        error(f"sprint_type_sequences 必须完整声明: {sorted(expected_sprint_types)}")
        sequences = {}
    for sprint_type, stages in sequences.items():
        stage_shape_valid = isinstance(stages, list)
        if stage_shape_valid:
            for index, stage in enumerate(stages):
                path = f"sprint_type_sequences.{sprint_type}[{index}]"
                if not isinstance(stage, dict):
                    error(f"{path} 必须是对象")
                    stage_shape_valid = False
                    continue
                unknown_keys = set(stage) - {"tasks", "require", "optional", "terminal_candidates"}
                if unknown_keys:
                    error(f"{path} 包含未知字段: {sorted(unknown_keys)}")
                    stage_shape_valid = False
                if "optional" in stage and type(stage["optional"]) is not bool:
                    error(f"{path}.optional 必须是布尔值")
                    stage_shape_valid = False
                if "terminal_candidates" in stage and (
                    not isinstance(stage["terminal_candidates"], list)
                    or not all(isinstance(item, str) and item for item in stage["terminal_candidates"])
                    or len(stage["terminal_candidates"]) != len(set(stage["terminal_candidates"]))
                ):
                    error(f"{path}.terminal_candidates 必须是无重复字符串数组")
                    stage_shape_valid = False
        stage_tasks = (
            [stage.get("tasks", []) if isinstance(stage, dict) else [] for stage in stages]
            if isinstance(stages, list)
            else []
        )
        flattened = [name for stage in stage_tasks for name in stage] if isinstance(stages, list) else []
        valid_names = all(isinstance(name, str) and name for name in flattened)
        if (
            not isinstance(stages, list)
            or not stage_shape_valid
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
            continue
        for index, stage in enumerate(stages):
            legacy_tasks = [
                name for name in stage["tasks"] if (tasks.get(name) or {}).get("lifecycle") == "legacy-only"
            ]
            if legacy_tasks and stage.get("optional") is not True:
                error(
                    f"sprint_type_sequences.{sprint_type}[{index}] 的 legacy-only 任务必须位于可选阶段: "
                    f"{sorted(legacy_tasks)}"
                )
        expected_terminal = {
            task for stage in stages for task in stage.get("terminal_candidates", []) if isinstance(task, str)
        }
        if (sprint_matrix.get(sprint_type) and not expected_terminal) or any(
            set(stage.get("terminal_candidates", [])) - set(stage["tasks"]) for stage in stages
        ):
            error(f"sprint_type_sequences.{sprint_type} 必须声明属于所在阶段的 terminal_candidates")
            continue
        configured_terminal = set((delivery_contracts.get(sprint_type) or {}).get("terminal_tasks", []))
        if configured_terminal != expected_terminal:
            error(
                f"sprint_delivery_contracts.{sprint_type}.terminal_tasks 必须等于最终阶段及 terminal_candidates: "
                f"{sorted(expected_terminal)}"
            )
    scope_routes = doc.get("scope_conflict_routes")
    responsible_scopes = {"story", "product", "design", "technical-design"}
    if not isinstance(scope_routes, dict) or set(scope_routes) != expected_sprint_types:
        error(f"scope_conflict_routes 必须完整声明: {sorted(expected_sprint_types)}")
        scope_routes = {}
    for sprint_type, routes in scope_routes.items():
        if not isinstance(routes, dict) or set(routes) != responsible_scopes:
            error(f"scope_conflict_routes.{sprint_type} 必须完整声明: {sorted(responsible_scopes)}")
            continue
        for scope, route in routes.items():
            path = f"scope_conflict_routes.{sprint_type}.{scope}"
            if not isinstance(route, dict) or len(route) != 1:
                error(f"{path} 必须且只能声明 owner_tasks 或 transfer_to")
                continue
            if "owner_tasks" in route:
                owners = route["owner_tasks"]
                if (
                    not isinstance(owners, list)
                    or not owners
                    or len(owners) != len(set(owners))
                    or not set(owners) <= set(sprint_matrix.get(sprint_type, []))
                ):
                    error(f"{path}.owner_tasks 必须引用当前 Sprint 可执行任务")
            elif "transfer_to" in route:
                target = route["transfer_to"]
                if target not in expected_sprint_types or target == sprint_type:
                    error(f"{path}.transfer_to 必须引用其他已知 Sprint 类型")
            else:
                error(f"{path} 必须声明 owner_tasks 或 transfer_to")
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
    type_matrix = doc.get("sprint_type_project_types")
    if not isinstance(type_matrix, dict) or set(type_matrix) != expected_sprint_types:
        error(f"sprint_type_project_types 必须完整声明: {sorted(expected_sprint_types)}")
    else:
        for sprint_type, project_types in type_matrix.items():
            if not isinstance(project_types, list) or not project_types or set(project_types) - PROJECT_TYPES:
                error(f"sprint_type_project_types.{sprint_type} 必须是有效 project_type 数组")
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
    # `tasks` 是任务类型唯一真源；能力矩阵和执行序列负责 fail-closed 覆盖校验。
    keywords = sorted(tasks, key=len, reverse=True)
    for name, task in tasks.items():
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
            if name in REGISTRY_TASKS:
                expected_directory = REGISTRY_TASKS[name][0]
                expected_index = f"docs/{expected_directory}/index.md"
                if index != expected_index:
                    error(f"tasks.{name}.outputs.index 必须使用作用域注册表: {expected_index}")
        allowed_project_types = task.get("allowed_project_types")
        if allowed_project_types is not None and (
            not isinstance(allowed_project_types, list)
            or not allowed_project_types
            or not set(allowed_project_types) <= PROJECT_TYPES
        ):
            error(f"tasks.{name}.allowed_project_types 必须是已知 project_type 的非空数组")
        declared_modes = task.get("allowed_modes")
        derived_modes = {mode for mode, names in mode_matrix.items() if name in names}
        if declared_modes is not None and set(declared_modes) != derived_modes:
            error(f"tasks.{name}.allowed_modes 与 mode_task_capabilities 不一致")
        common_acceptance = validate_acceptance(task.get("acceptance"), f"tasks.{name}.acceptance", error, warning)
        if task.get("execution_protocol") not in {None, "agent", "action", "orchestrator"}:
            error(f"tasks.{name}.execution_protocol 非法")
        if task.get("review_protocol") not in {None, "agent-full", "artifact-only"}:
            error(f"tasks.{name}.review_protocol 非法")
        if task.get("execution_contract") not in {None, "integration-v1"}:
            error(f"tasks.{name}.execution_contract 非法")
        if (
            task.get("execution_contract") == "integration-v1"
            and task.get("artifact_action") != "project.integration.guard"
        ):
            error(f"tasks.{name}.execution_contract=integration-v1 必须使用 project.integration.guard")
        if (
            task.get("execution_contract") == "integration-v1"
            and task.get("entry_action") != "project.integration.execute"
        ):
            error(f"tasks.{name}.execution_contract=integration-v1 必须使用 project.integration.execute")
        if name == "promote-test" and task.get("artifact_action") != "project.promote-test.guard":
            error("tasks.promote-test 必须使用 project.promote-test.guard 校验当前部署 attempt")
        if task.get("lifecycle") not in {None, "legacy-only"}:
            error(f"tasks.{name}.lifecycle 只允许 legacy-only")
        acceptance_by_project_type = task.get("acceptance_by_project_type")
        if acceptance_by_project_type is not None:
            if not isinstance(acceptance_by_project_type, dict) or not set(acceptance_by_project_type) <= PROJECT_TYPES:
                error(f"tasks.{name}.acceptance_by_project_type 只能声明已知 project_type")
            else:
                for project_type, items in acceptance_by_project_type.items():
                    type_ids = validate_acceptance(
                        items, f"tasks.{name}.acceptance_by_project_type.{project_type}", error, warning
                    )
                    if set(common_acceptance) & set(type_ids):
                        error(f"tasks.{name}.acceptance 与 {project_type} acceptance ID 重复")
        facets = task.get("facets")
        if facets is not None and (
            not isinstance(facets, list)
            or not facets
            or not all(isinstance(item, str) and item for item in facets)
            or len(set(facets)) != len(facets)
        ):
            error(f"tasks.{name}.facets 必须是唯一非空字符串数组")
            facets = []
        default_facets = task.get("default_facets")
        if default_facets is not None and (
            not isinstance(default_facets, list) or not set(default_facets) <= set(facets or [])
        ):
            error(f"tasks.{name}.default_facets 必须是 facets 的子集")
        defaults_by_type = task.get("default_facets_by_project_type")
        if defaults_by_type is not None:
            if not isinstance(defaults_by_type, dict) or not set(defaults_by_type) <= PROJECT_TYPES:
                error(f"tasks.{name}.default_facets_by_project_type 只能声明已知 project_type")
            elif any(
                not isinstance(items, list) or not set(items) <= set(facets or [])
                for items in defaults_by_type.values()
            ):
                error(f"tasks.{name}.default_facets_by_project_type 必须只引用已声明 facets")
        acceptance_by_facet = task.get("acceptance_by_facet")
        if acceptance_by_facet is not None:
            if not isinstance(acceptance_by_facet, dict) or not set(acceptance_by_facet) <= set(facets or []):
                error(f"tasks.{name}.acceptance_by_facet 只能引用已声明 facets")
            else:
                used_ids = set(common_acceptance)
                for facet, items in acceptance_by_facet.items():
                    facet_ids = validate_acceptance(items, f"tasks.{name}.acceptance_by_facet.{facet}", error, warning)
                    if used_ids & set(facet_ids):
                        error(f"tasks.{name}.acceptance_by_facet.{facet} acceptance ID 重复")
                    used_ids.update(facet_ids)
        tools = task.get("tools")
        if isinstance(tools, dict):
            for key in ("allow", "deny"):
                if not isinstance(tools.get(key), list):
                    error(f"tasks.{name}.tools.{key} 必须是数组")
        for legacy in ("infra_ready", "entry_command", "artifact_guard"):
            if legacy in task:
                error(f"tasks.{name}.{legacy} 已废弃，必须使用 Action Registry 字段")
        if task.get("infra_cache") not in {None, "ttl", "never"}:
            error(f"tasks.{name}.infra_cache 只允许 ttl/never")
        if "infra_cache" in task and not task.get("infra_action"):
            error(f"tasks.{name}.infra_cache 只能与 infra_action 同时声明")
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
                    task_modes = set(task.get("allowed_modes") or derived_modes)
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
                source_types = set((tasks.get(keyword) or {}).get("allowed_project_types") or PROJECT_TYPES)
                target_types = set(task.get("allowed_project_types") or PROJECT_TYPES)
                if not source_types & target_types:
                    error(f"tasks.{name}.prerequisites 与 {keyword} 无可共同执行的 project_type")
        for group in task.get("prerequisites_any", []):
            if not isinstance(group, list) or not group or not all(item in tasks for item in group):
                error(f"tasks.{name}.prerequisites_any 必须引用已定义任务")
                continue
            target_types = set(task.get("allowed_project_types") or PROJECT_TYPES)
            for project_type in target_types:
                if not any(
                    project_type in set((tasks[item] or {}).get("allowed_project_types") or PROJECT_TYPES)
                    for item in group
                ):
                    error(f"tasks.{name}.prerequisites_any 在 project_type={project_type} 无可达前置")
        external = task.get("external_evidence")
        if external is not None and (
            not isinstance(external, dict)
            or bool(
                set(external)
                - {
                    "kind",
                    "field",
                    "sprint_types",
                    "candidate_field",
                    "require_head_match",
                    "require_source_ancestry",
                }
            )
            or external.get("kind") != "sprint-signoffs"
            or not isinstance(external.get("field"), str)
            or (
                "candidate_field" in external
                and (
                    not isinstance(external["candidate_field"], str)
                    or not external["candidate_field"]
                    or type(external.get("require_head_match")) is not bool
                    or type(external.get("require_source_ancestry")) is not bool
                )
            )
            or ("candidate_field" not in external and set(external) & {"require_head_match", "require_source_ancestry"})
            or (
                "sprint_types" in external
                and (
                    not isinstance(external["sprint_types"], list)
                    or not external["sprint_types"]
                    or len(external["sprint_types"]) != len(set(external["sprint_types"]))
                    or bool(set(external["sprint_types"]) - expected_sprint_types)
                )
            )
        ):
            error(
                f"tasks.{name}.external_evidence 必须声明 kind=sprint-signoffs、field，"
                "且可选 Sprint 范围与 candidate commit 约束必须完整合法"
            )
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

"""Shared validation for versioned quality execution evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

EXECUTION_EVIDENCE_VERSION = 2
EXECUTION_OUTCOMES = {"passed", "failed", "not_applicable", "deferred", "blocked"}
ZERO_EXECUTION_OUTCOMES = {"not_applicable", "deferred", "blocked"}
GROUP_FIELDS = {
    "executedCases": "executed_dispositions",
    "deferredCases": "deferred_dispositions",
    "excludedCases": "excluded_dispositions",
    "liveCases": "live_dispositions",
}
POLICY_FIELDS = {
    "enabled",
    "execution_evidence_version",
    "e2e_artifact",
    *GROUP_FIELDS.values(),
    "passing_dispositions",
    "zero_execution_profiles",
    "zero_execution_outcomes",
}


def safe_json_path(value: Any) -> bool:
    """Return whether value names a project-relative JSON file."""
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and path.suffix == ".json"


def _string_list(policy: Mapping[str, Any], field: str, errors: list[str]) -> list[str]:
    values = policy.get(field, [])
    prefix = f"quality.action_evidence.execution_policy.{field}"
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        errors.append(f"{prefix}: 必须是非空字符串列表")
        return []
    if len(values) != len(set(values)):
        errors.append(f"{prefix}: 值不得重复")
    return values


def validate_execution_policy(policy: Any, *, evidence_enabled: bool) -> list[str]:
    """Validate the strict v1/v2 execution-policy configuration."""
    prefix = "quality.action_evidence.execution_policy"
    if not isinstance(policy, dict):
        return [f"{prefix}: 必须是对象"]
    errors: list[str] = []
    if unknown := set(policy) - POLICY_FIELDS:
        errors.append(f"{prefix}: 未知字段 {sorted(unknown)}")
    enabled = policy.get("enabled", False)
    if not isinstance(enabled, bool):
        errors.append(f"{prefix}.enabled: 必须是 boolean")
        enabled = False
    version = policy.get("execution_evidence_version", 1)
    if type(version) is not int or version not in {1, EXECUTION_EVIDENCE_VERSION}:
        errors.append(f"{prefix}.execution_evidence_version: 必须是 1 或 {EXECUTION_EVIDENCE_VERSION}")
        version = 1
    lists = {field: _string_list(policy, field, errors) for field in GROUP_FIELDS.values()}
    lists["passing_dispositions"] = _string_list(policy, "passing_dispositions", errors)
    lists["zero_execution_profiles"] = _string_list(policy, "zero_execution_profiles", errors)
    lists["zero_execution_outcomes"] = _string_list(policy, "zero_execution_outcomes", errors)
    if enabled and not evidence_enabled:
        errors.append(f"{prefix}: 只能随已启用的 action evidence 使用")
    if version == 1:
        _validate_v1_policy(enabled, policy, lists, errors)
    else:
        _validate_v2_policy(enabled, policy, lists, errors)
    return errors


def _validate_v1_policy(
    enabled: bool, policy: Mapping[str, Any], lists: Mapping[str, list[str]], errors: list[str]
) -> None:
    prefix = "quality.action_evidence.execution_policy"
    executed = lists["executed_dispositions"]
    deferred = lists["deferred_dispositions"]
    v2_fields = (
        "e2e_artifact",
        "excluded_dispositions",
        "live_dispositions",
        "passing_dispositions",
        "zero_execution_profiles",
        "zero_execution_outcomes",
    )
    if enabled and (not executed or not deferred):
        errors.append(f"{prefix}: v1 启用时 executed/deferred disposition 必须非空")
    if set(executed) & set(deferred):
        errors.append(f"{prefix}: executed/deferred disposition 必须互斥")
    if not enabled and (executed or deferred):
        errors.append(f"{prefix}: 未启用时 disposition 声明必须为空")
    if any(policy.get(field) not in (None, "", []) for field in v2_fields):
        errors.append(f"{prefix}: v2 字段要求 execution_evidence_version=2")


def _validate_v2_policy(
    enabled: bool, policy: Mapping[str, Any], lists: Mapping[str, list[str]], errors: list[str]
) -> None:
    prefix = "quality.action_evidence.execution_policy"
    if not enabled:
        errors.append(f"{prefix}: v2 必须显式 enabled=true")
    if not safe_json_path(policy.get("e2e_artifact")):
        errors.append(f"{prefix}.e2e_artifact: 必须是安全的工程内 JSON 路径")
    disposition_fields = [*GROUP_FIELDS.values()]
    groups = [lists[field] for field in disposition_fields]
    if any(not group for group in groups):
        errors.append(f"{prefix}: v2 四分区 disposition 必须非空")
    flattened = [item for group in groups for item in group]
    if len(flattened) != len(set(flattened)):
        errors.append(f"{prefix}: 四分区 disposition 必须互斥")
    passing = set(lists["passing_dispositions"])
    if not passing or not passing <= set(flattened):
        errors.append(f"{prefix}.passing_dispositions: 必须是已声明 disposition 的非空子集")
    if not lists["zero_execution_profiles"]:
        errors.append(f"{prefix}.zero_execution_profiles: v2 必须显式声明")
    outcomes = set(lists["zero_execution_outcomes"])
    if not outcomes or not outcomes <= ZERO_EXECUTION_OUTCOMES:
        errors.append(f"{prefix}.zero_execution_outcomes: 只能声明 not_applicable/deferred/blocked")


def _indexed(items: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError(f"quality execution evidence {label} 分区缺失")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError(f"quality execution evidence {label} case 格式无效")
        if item["id"] in result:
            raise ValueError(f"quality execution evidence {label} case 重复")
        result[item["id"]] = item
    return result


def _require_int(summary: Mapping[str, Any], field: str, expected: int) -> None:
    if type(summary.get(field)) is not int or summary[field] != expected:
        raise ValueError(f"quality execution evidence executionSummary.{field} 无效")


def validate_execution_evidence(
    document: Any, policy: Mapping[str, Any], expected_run_id: str
) -> dict[str, Any]:
    """Validate and normalize one v2 execution evidence document."""
    if not isinstance(document, dict) or document.get("executionEvidenceVersion") != EXECUTION_EVIDENCE_VERSION:
        raise ValueError("quality execution evidence version 无效")
    profile = document.get("profile")
    reasons = document.get("reasons")
    outcome = document.get("executionOutcome")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("quality execution evidence profile 无效")
    if document.get("runId") != expected_run_id:
        raise ValueError("quality execution evidence run ID 无效")
    if not isinstance(document.get("evidenceValid"), bool) or not isinstance(document.get("passed"), bool):
        raise ValueError("quality execution evidence validity/pass 标记无效")
    if outcome not in EXECUTION_OUTCOMES or not isinstance(reasons, list) or not reasons:
        raise ValueError("quality execution evidence outcome/reasons 无效")
    if any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
        raise ValueError("quality execution evidence reasons 必须为非空字符串")
    selected = _indexed(document.get("selectedCases"), "selected")
    groups = {field: _indexed(document.get(field), field) for field in GROUP_FIELDS}
    _validate_partitions(selected, groups, policy)
    score_eligible = _validate_summary_and_outcome(document, groups, policy)
    normalized = dict(document)
    normalized["scoreEligible"] = score_eligible
    normalized["loadStatus"] = "passed" if document["passed"] else "valid_not_passed"
    return normalized


def _validate_partitions(
    selected: Mapping[str, dict[str, Any]],
    groups: Mapping[str, Mapping[str, dict[str, Any]]],
    policy: Mapping[str, Any],
) -> None:
    seen: set[str] = set()
    combined: dict[str, dict[str, Any]] = {}
    for field, disposition_field in GROUP_FIELDS.items():
        allowed = set(policy.get(disposition_field, []))
        group = groups[field]
        if seen & set(group):
            raise ValueError("quality execution evidence 四分区不互斥")
        seen.update(group)
        combined.update(group)
        if any(item.get("disposition") not in allowed for item in group.values()):
            raise ValueError(f"quality execution evidence {field} disposition 无效")
    if set(selected) != seen or any(selected[case_id] != combined[case_id] for case_id in selected):
        raise ValueError("quality execution evidence 四分区未精确覆盖 selectedCases")


def _validate_summary_and_outcome(
    document: Mapping[str, Any],
    groups: Mapping[str, Mapping[str, dict[str, Any]]],
    policy: Mapping[str, Any],
) -> bool:
    summary = document.get("executionSummary")
    if not isinstance(summary, dict):
        raise ValueError("quality execution evidence executionSummary 缺失")
    executed = list(groups["executedCases"].values())
    passed_count = sum(item.get("status") == "pass" for item in executed)
    failed_count = sum(item.get("status") == "fail" for item in executed)
    if passed_count + failed_count != len(executed):
        raise ValueError("quality execution evidence executed status 无效")
    counts = {
        "selectedCaseCount": sum(len(group) for group in groups.values()),
        "executedCaseCount": len(executed),
        "passedCaseCount": passed_count,
        "failedCaseCount": failed_count,
        "deferredCaseCount": len(groups["deferredCases"]),
        "excludedCaseCount": len(groups["excludedCases"]),
        "liveCaseCount": len(groups["liveCases"]),
    }
    for field, expected in counts.items():
        _require_int(summary, field, expected)
    runner_count = summary.get("runnerInvocationCount")
    if type(runner_count) is not int or runner_count < 0:
        raise ValueError("quality execution evidence runnerInvocationCount 无效")
    passing = set(policy.get("passing_dispositions", []))
    all_items = [item for group in groups.values() for item in group.values()]
    if any(item.get("status") == "pass" and item.get("disposition") not in passing for item in all_items):
        raise ValueError("quality execution evidence passing disposition 无效")
    score_eligible = bool(
        document["evidenceValid"]
        and document["passed"]
        and document["executionOutcome"] == "passed"
        and executed
        and runner_count > 0
        and failed_count == 0
        and passed_count == len(executed)
    )
    if document["passed"] and not score_eligible:
        raise ValueError("quality execution evidence passed 与执行计数不一致")
    if not executed:
        _validate_zero_execution(document, policy, runner_count, passed_count, failed_count)
    elif runner_count == 0:
        raise ValueError("quality execution evidence 非零执行缺少 runner")
    return score_eligible


def _validate_zero_execution(
    document: Mapping[str, Any], policy: Mapping[str, Any], runner: int, passed: int, failed: int
) -> None:
    if (
        document["profile"] not in set(policy.get("zero_execution_profiles", []))
        or document["executionOutcome"] not in set(policy.get("zero_execution_outcomes", []))
        or document["evidenceValid"] is not True
        or document["passed"] is not False
        or runner != 0
        or passed != 0
        or failed != 0
    ):
        raise ValueError("quality execution evidence zero-execution 组合无效")


def assert_matching_execution_evidence(action: Mapping[str, Any], e2e: Mapping[str, Any]) -> None:
    """Require an action manifest to preserve the producer execution model exactly."""
    fields = [
        "executionEvidenceVersion",
        "runId",
        "profile",
        "evidenceValid",
        "passed",
        "executionOutcome",
        "reasons",
        "selectedCases",
        *GROUP_FIELDS,
        "executionSummary",
    ]
    if any(action.get(field) != e2e.get(field) for field in fields):
        raise ValueError("quality action evidence 与 E2E artifact 不一致")

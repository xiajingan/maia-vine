#!/usr/bin/env python3
"""Validate walkthrough artifacts and persist Boss approval decisions."""

import argparse
import hashlib
import re
from datetime import datetime
from pathlib import Path

from mai_harness.runtime.application.requirements import record_feedback
from mai_harness.runtime.commands.task_rollback import reopen_scope, rollback
from mai_harness.runtime.domain.modes import PROJECT_TYPES
from mai_harness.runtime.domain.scope_routing import resolve_scope_route
from mai_harness.runtime.domain.sprint_context import (
    build_sprint_outcome_receipt,
    planning_contract_digest,
    sprint_header,
    sprint_outcome_path,
    sprint_planning_contract,
    sprint_uses_story_requirements,
    table_rows,
)
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run, write_yaml_bundle

LEGACY_BOSS_APPROVAL_ACCEPTANCE = "Boss 已批准 legacy Sprint 的当前验收结果"
LEGACY_BOSS_APPROVAL_OBSERVED = "Boss 明确确认当前验收报告通过"


def _acceptance_task_id(plan: Path) -> str:
    acceptance_ids = [
        str(row.get("id"))
        for row in table_rows(plan.read_text(encoding="utf-8"))
        if (row.get("类型") or row.get("type")) == "product-acceptance" and row.get("id")
    ]
    if len(acceptance_ids) != 1:
        raise ValueError("Sprint 必须且只能有一个带真实任务 ID 的 product-acceptance 任务")
    return acceptance_ids[0]


def _story_rejection_update(root: Path, plan: Path, reason: str) -> str:
    rules = load_yaml(HarnessPaths.detect(project=root).rules / "task-rules.yml")
    sprint_type = sprint_header(plan).get("sprint_type", "")
    route = resolve_scope_route(rules, sprint_type, "story")
    if transfer_to := route.get("transfer_to"):
        raise ValueError(f"当前 {sprint_type} 的 Story 反馈必须转入新的 {transfer_to}")
    owners = set(route.get("owner_tasks", ()))
    stages = (rules.get("sprint_type_sequences") or {}).get(sprint_type) or []
    updated, _ = reopen_scope(
        plan.read_text(encoding="utf-8"),
        stages,
        _acceptance_task_id(plan),
        "story",
        owners,
        reason,
    )
    return updated


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def paths_for(root: Path, sprint: str) -> dict[str, Path]:
    series = (re.match(r"^sprint-\d+", sprint) or re.match(r".*", sprint)).group(0)
    directory = root / "docs/acceptance-reports"

    def resolve(suffix: str) -> Path:
        for name in dict.fromkeys((sprint, series)):
            if (directory / f"{name}{suffix}").exists():
                return directory / f"{name}{suffix}"
        return directory / f"{sprint}{suffix}"

    return {
        "walkthrough": resolve("-walkthrough.md"),
        "acceptance": resolve("-acceptance.md"),
        "signoff": resolve("-boss-signoff.yml"),
    }


def sprint_plan(root: Path, sprint: str) -> Path | None:
    for status in ("active", "completed"):
        candidate = root / "docs/exec-plans" / status / f"{sprint}.md"
        if candidate.is_file():
            return candidate
    return None


def lint_contract_evidence(root: Path, sprint: str, files: dict[str, Path], require_approved: bool) -> list[str]:
    plan = sprint_plan(root, sprint)
    if plan is None:
        return [f"Sprint 计划不存在: {sprint}"]
    contract = sprint_planning_contract(plan)
    if contract["planning_contract_version"] is None:  # Legacy active/completed Sprint.
        return []
    errors: list[str] = []
    walkthrough = files["walkthrough"].read_text(encoding="utf-8") if files["walkthrough"].is_file() else ""
    observation = contract.get("boss_observation")
    acceptance = contract.get("outcome_acceptance")
    evidence = []
    if isinstance(observation, dict):
        evidence.extend(str(observation.get(field, "")).strip() for field in ("entrypoint", "action"))
    if isinstance(acceptance, list):
        evidence.extend(str(item).strip() for item in acceptance)
    for item in filter(None, evidence):
        if item not in walkthrough:
            errors.append(f"走查指南未覆盖 Sprint 交付契约: {item}")
    if sprint_uses_story_requirements(sprint_header(plan).get("sprint_type", ""), contract):
        trace_text = (
            walkthrough
            + "\n"
            + (files["acceptance"].read_text(encoding="utf-8") if files["acceptance"].is_file() else "")
        )
        for source in contract.get("source_stories") or []:
            if not isinstance(source, dict):
                continue
            for identifier in [source.get("id"), *(source.get("acs") or [])]:
                if isinstance(identifier, str) and identifier not in trace_text:
                    errors.append(f"产品走查未追溯 Sprint 需求输入: {identifier}")
    if require_approved and files["signoff"].is_file():
        record = load_yaml(files["signoff"])
        actual = record.get("planning_contract_sha256") if isinstance(record, dict) else None
        expected = planning_contract_digest(plan)
        if actual != expected:
            errors.append(f"审批记录未绑定当前 Sprint 交付契约: {actual or '缺失'} != {expected}")
    return errors


def lint_artifacts(
    files: dict[str, Path],
    require_signoff: bool,
    require_approved: bool,
    *,
    root: Path | None = None,
    sprint: str = "",
) -> list[str]:
    errors = []
    requirements = {
        "walkthrough": (
            "## 环境信息",
            ("## 变更对照", "## 设计对照"),
            "## 功能走查路径",
            ("## 呈现质量检查", "## 版式整洁度检查"),
            "预期结果",
        ),
        "acceptance": (
            "## Boss 走查记录",
            "## 偏差清单",
            "## 结论",
            "### Critical",
            "### Major",
            "### Minor",
            "### Observation",
        ),
    }
    for kind, markers in requirements.items():
        if not files[kind].exists():
            errors.append(f"{kind} 不存在: {files[kind]}")
            continue
        text = files[kind].read_text(encoding="utf-8")
        for marker in markers:
            alternatives = marker if isinstance(marker, tuple) else (marker,)
            if not any(value in text for value in alternatives):
                errors.append(f"{files[kind]} 缺少 {' 或 '.join(alternatives)}")
            headings = [value for value in alternatives if value.startswith("#")]
            if headings and sum(len(re.findall(rf"(?m)^{re.escape(value)}\s*$", text)) for value in headings) != 1:
                errors.append(f"{files[kind]} 章节必须且只能声明一个: {' 或 '.join(headings)}")
    if require_signoff or files["signoff"].exists():
        if not files["signoff"].exists():
            errors.append(f"审批记录不存在: {files['signoff']}")
        else:
            record = load_yaml(files["signoff"])
            if not isinstance(record, dict):
                errors.append(f"审批记录格式非法（顶层必须是对象）: {files['signoff']}")
                record = {}
            for key in ("sprint", "decision", "confirmed_by", "confirmed_at", "source") + (
                ("commit_sha",) if require_approved else ()
            ):
                if not record.get(key):
                    errors.append(f"审批记录缺少字段: {key}")
            if require_approved and record.get("decision") != "approved":
                errors.append("审批记录未放行")
    if root is not None and sprint:
        errors.extend(lint_contract_evidence(root, sprint, files, require_approved))
    return errors


def _check_coverage(
    text: str,
    rules: dict,
    planning: dict | None = None,
    project_type: str = "",
) -> list[str]:
    sections = list(re.finditer(r"(?ms)^## 检查项结果\s*$([\s\S]*?)(?=^## |\Z)", text))
    if len(sections) != 1:
        if len(sections) > 1:
            return ["走查报告 ## 检查项结果 必须且只能声明一次"]
        return ["走查报告缺少 ## 检查项结果"]
    rows: list[dict[str, str]] = []
    lines = sections[0].group(1).splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|") or index + 1 >= len(lines):
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        aliases = {
            "id": next((name for name in headers if name.casefold() in {"check id", "检查项", "id"}), ""),
            "result": next((name for name in headers if name.casefold() in {"result", "结果"}), ""),
            "evidence": next(
                (name for name in headers if name.casefold() in {"evidence", "依据", "范围依据/证据"}), ""
            ),
        }
        if not all(aliases.values()):
            continue
        separator = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
        if len(separator) != len(headers) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            continue
        for value in lines[index + 2 :]:
            if not value.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                original = dict(zip(headers, cells, strict=True))
                rows.append({key: original[name] for key, name in aliases.items()})
        break
    definitions = {
        item.get("id"): item
        for dimension in (rules.get("dimensions") or {}).values()
        if isinstance(dimension, dict)
        for item in dimension.get("checks", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    expected = set(definitions)
    errors: list[str] = []
    invalid_applicability = sorted(
        check_id
        for check_id, definition in definitions.items()
        if definition.get("applicability") not in {"required", "conditional"}
    )
    if invalid_applicability:
        errors.append(f"walkthrough-checks applicability 非法或缺失: {invalid_applicability}")
    impacts = set((planning or {}).get("impact_surfaces") or {})
    for check_id in sorted(expected):
        matches = [row for row in rows if row["id"] == check_id]
        if len(matches) != 1:
            errors.append(f"检查项结果必须唯一覆盖: {check_id}")
            continue
        result = matches[0]["result"].strip().casefold()
        evidence = matches[0]["evidence"].strip()
        if result not in {"pass", "通过", "n/a", "na", "不适用"}:
            errors.append(f"检查项 {check_id} 结果必须为 pass 或 n/a")
        is_na = result in {"n/a", "na", "不适用"}
        definition = definitions[check_id]
        triggers = definition.get("applies_when")
        triggered = False
        if definition.get("applicability") == "conditional":
            trigger_valid = isinstance(triggers, dict) and bool(triggers)
            if trigger_valid:
                trigger_valid = (
                    not (set(triggers) - {"impact_surfaces", "project_types"})
                    and all(
                        isinstance(values, list)
                        and values
                        and all(isinstance(value, str) and value for value in values)
                        for values in triggers.values()
                    )
                    and not set(triggers.get("project_types", [])) - PROJECT_TYPES
                )
            if not trigger_valid:
                errors.append(f"条件检查项 {check_id} 缺少可执行 applies_when")
                triggered = True
            else:
                trigger_impacts = set(triggers.get("impact_surfaces") or [])
                trigger_project_types = set(triggers.get("project_types") or [])
                triggered = (not trigger_impacts or bool(impacts & trigger_impacts)) and (
                    not trigger_project_types or project_type in trigger_project_types
                )
        if is_na and (definition.get("applicability") != "conditional" or triggered):
            errors.append(f"必选检查项 {check_id} 不允许标记为 n/a")
        if is_na and re.fullmatch(r"(?i)\s*(?:n/?a|不适用|本次不适用|无)\s*", evidence):
            errors.append(f"条件检查项 {check_id} 的 n/a 必须说明具体范围依据")
        if len(evidence) < 4 or re.search(r"(?i)\b(?:todo|tbd)\b|待补|占位", evidence):
            errors.append(f"检查项 {check_id} 缺少具体证据或不适用依据")
    unknown = {row["id"] for row in rows} - expected
    if unknown:
        errors.append(f"检查项结果包含规则未声明 ID: {sorted(unknown)}")
    return errors


def score_report(
    path: Path,
    rules_path: Path,
    *,
    require_check_coverage: bool = False,
    planning: dict | None = None,
    project_type: str = "",
) -> list[str]:
    text = path.read_text(encoding="utf-8")
    document = load_yaml(rules_path)
    rules = document.get("pass_rules", {})
    failures = []

    def count(label: str) -> int:
        match = re.search(rf"###\s+{label}[^\n]*\n([\s\S]*?)(?=\n###\s+|\n##\s+|$)", text)
        return (
            len(re.findall(r"^\s*-\s+\S", match.group(1), re.M))
            if match and not re.search(r"^\s*(无|none|n/a)\s*$", match.group(1), re.I | re.M)
            else 0
        )

    for label, key, default in (("Critical", "critical_max", 0), ("Major", "major_max", 0), ("Minor", "minor_max", 5)):
        value = count(label)
        maximum = rules.get(key, default)
        if value > maximum:
            failures.append(f"{label} {value} > {maximum}")
    score_matches = re.findall(r"\bP-?([1-5])\b[^\n|]*?(\d(?:\.\d)?)\s*/\s*5", text, re.I)
    by_id: dict[str, list[float]] = {str(index): [] for index in range(1, 6)}
    for identifier, value in score_matches:
        score = float(value)
        if 0 <= score <= 5:
            by_id[identifier].append(score)
    invalid_ids = [f"P-{identifier}" for identifier, values in by_id.items() if len(values) != 1]
    if invalid_ids:
        failures.append(f"产品决策评分必须 P-1~P-5 各且仅各一项: {invalid_ids}")
    scores = [values[0] for values in by_id.values() if len(values) == 1]
    average = sum(scores) / len(scores) if len(scores) == 5 else None
    minimum = rules.get("product_decision_min", rules.get("product_stance_min", 4))
    if average is None or average < minimum:
        failures.append("产品决策质量未评分或低于门槛")
    if require_check_coverage:
        failures.extend(_check_coverage(text, document, planning, project_type))
    return failures


def verify_artifacts(root: Path, sprint: str, files: dict[str, Path], require_approved: bool) -> list[str]:
    errors = lint_artifacts(files, require_approved, require_approved, root=root, sprint=sprint)
    plan = sprint_plan(root, sprint)
    require_coverage = bool(
        plan is not None and sprint_planning_contract(plan).get("planning_contract_version") in {2, 3}
    )
    planning = sprint_planning_contract(plan) if plan is not None else None
    project_type = load_harness_config(force=True, path=root / "config/harness.yml")["project"]["type"]
    if files["acceptance"].is_file():
        errors.extend(
            score_report(
                files["acceptance"],
                HarnessPaths.detect(project=root).rules / "walkthrough-checks.yml",
                require_check_coverage=require_coverage,
                planning=planning,
                project_type=project_type,
            )
        )
    else:
        errors.append("走查报告不存在")
    return errors


def boss_outcome_attestation(root: Path, plan: Path, acceptance_path: Path) -> tuple[dict, list[str]]:
    """Parse the Boss-owned expected/observed/result table into a signed attestation."""
    text = acceptance_path.read_text(encoding="utf-8")
    sections = list(re.finditer(r"(?ms)^## Boss 走查记录\s*$([\s\S]*?)(?=^## |\Z)", text))
    errors: list[str] = []
    if len(sections) != 1:
        errors.append("走查报告 ## Boss 走查记录 必须且只能声明一次")
    lines = (sections[0].group(1) if sections else "").splitlines()
    rows: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|") or index + 1 >= len(lines):
            continue
        headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
        separator = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
        if not {"验收条件", "实际观察", "结果"} <= set(headers) or not all(
            re.fullmatch(r":?-{3,}:?", cell) for cell in separator
        ):
            continue
        for value in lines[index + 2 :]:
            if not value.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells, strict=True)))
        break
    contract = sprint_planning_contract(plan)
    expected = contract.get("outcome_acceptance")
    clauses = expected if isinstance(expected, list) else []
    evidence_path = acceptance_path.resolve().relative_to(root.resolve()).as_posix()
    evidence_sha = hashlib.sha256(acceptance_path.read_bytes()).hexdigest()
    evidence: list[dict[str, str]] = []
    if contract["planning_contract_version"] is None and not clauses:
        evidence.append(
            {
                "acceptance": LEGACY_BOSS_APPROVAL_ACCEPTANCE,
                "path": evidence_path,
                "sha256": evidence_sha,
                "observed": LEGACY_BOSS_APPROVAL_OBSERVED,
                "result": "pass",
            }
        )
    for clause in clauses:
        matches = [row for row in rows if row.get("验收条件") == clause]
        if len(matches) != 1:
            errors.append(f"Boss 走查记录必须唯一覆盖验收条件: {clause}")
            continue
        observed = matches[0].get("实际观察", "").strip()
        result = matches[0].get("结果", "").strip().lower()
        if result not in {"pass", "通过"}:
            errors.append(f"Boss 走查结果未通过: {clause}")
            continue
        normalized_observed = re.sub(r"\s+", "", observed).casefold()
        normalized_clause = re.sub(r"\s+", "", clause).casefold()
        if not observed or normalized_clause in normalized_observed:
            errors.append(f"Boss 走查实际观察必须是独立具体结果，不能留空或复述验收条件: {clause}")
            continue
        evidence.append(
            {
                "acceptance": clause,
                "path": evidence_path,
                "sha256": evidence_sha,
                "observed": observed,
                "result": "pass",
            }
        )
    return (
        {
            "planning_contract_sha256": planning_contract_digest(plan),
            "result": "passed",
            "evidence": evidence,
        },
        errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("lint", "score", "verify", "approve", "reject"))
    parser.add_argument("sprint")
    parser.add_argument("--require-signoff", action="store_true")
    parser.add_argument("--require-approved", action="store_true")
    parser.add_argument("--by", default="Boss")
    parser.add_argument("--summary", default="")
    parser.add_argument("--commit-sha", default="")
    parser.add_argument(
        "--feedback-classification",
        choices=("implementation-defect", "requirement-change", "new-story", "non-product"),
    )
    parser.add_argument("--story-input", type=Path)
    parser.add_argument("--feedback-disposition", choices=("current-sprint", "backlog"), default="current-sprint")
    args = parser.parse_args()
    root = Path.cwd()
    files = paths_for(root, args.sprint)
    if args.action == "lint":
        errors = lint_artifacts(files, args.require_signoff, args.require_approved, root=root, sprint=args.sprint)
    elif args.action == "score":
        plan = sprint_plan(root, args.sprint)
        errors = (
            score_report(
                files["acceptance"],
                HarnessPaths.detect(project=root).rules / "walkthrough-checks.yml",
                require_check_coverage=bool(
                    plan is not None and sprint_planning_contract(plan).get("planning_contract_version") in {2, 3}
                ),
                planning=sprint_planning_contract(plan) if plan is not None else None,
                project_type=load_harness_config(force=True, path=root / "config/harness.yml")["project"]["type"],
            )
            if files["acceptance"].exists()
            else ["走查报告不存在"]
        )
    elif args.action == "verify":
        errors = verify_artifacts(root, args.sprint, files, True)
    else:
        errors = lint_artifacts(files, False, False, root=root, sprint=args.sprint)
        if args.action == "approve" and files["acceptance"].is_file():
            plan_for_score = sprint_plan(root, args.sprint)
            errors.extend(
                score_report(
                    files["acceptance"],
                    HarnessPaths.detect(project=root).rules / "walkthrough-checks.yml",
                    require_check_coverage=bool(
                        plan_for_score is not None
                        and sprint_planning_contract(plan_for_score).get("planning_contract_version") in {2, 3}
                    ),
                    planning=sprint_planning_contract(plan_for_score) if plan_for_score is not None else None,
                    project_type=load_harness_config(force=True, path=root / "config/harness.yml")["project"]["type"],
                )
            )
        plan = sprint_plan(root, args.sprint)
        outcome_receipt: dict = {}
        if args.action == "approve" and plan is not None:
            evidence_path = files["acceptance"].resolve().relative_to(root.resolve()).as_posix()
            signoff_path = (root / "docs/acceptance-reports" / f"{args.sprint}-boss-signoff.yml").relative_to(root)
            attestation, observation_errors = boss_outcome_attestation(root, plan, files["acceptance"])
            errors.extend(observation_errors)
            outcome_receipt, outcome_errors = build_sprint_outcome_receipt(
                root,
                plan,
                [evidence_path],
                {"kind": "boss-signoff", "path": signoff_path.as_posix()},
                attestation,
            )
            errors.extend(outcome_errors)
        if errors:
            for error in errors:
                print(f"❌ {error}")
            return 1
        sha = args.commit_sha or try_run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
        current_sha = try_run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
        if args.action == "approve" and current_sha and sha != current_sha:
            print("❌ approved commit_sha 必须等于当前 Git HEAD")
            return 1
        if args.action == "approve" and not sha:
            print("❌ approved 必须有 commit_sha")
            return 1
        if args.action == "reject" and not args.feedback_classification:
            print("❌ reject 必须使用 --feedback-classification 完成反馈分诊")
            return 1
        if (
            args.action == "approve"
            and args.feedback_classification
            and not (
                args.feedback_classification in {"new-story", "non-product"} and args.feedback_disposition == "backlog"
            )
        ):
            print("❌ approve 只允许把 new-story/non-product 观察项记录到 backlog")
            return 1
        if args.action == "reject" and args.feedback_disposition != "current-sprint":
            print("❌ reject 的反馈必须在 current-sprint 闭环；后续优化应批准当前结果并进入 backlog")
            return 1
        target = root / "docs/acceptance-reports" / f"{args.sprint}-boss-signoff.yml"
        outcome_target = sprint_outcome_path(root, args.sprint)
        active_plan = root / "docs/exec-plans/active" / f"{args.sprint}.md"
        rejection_reason = args.summary or "Boss 走查反馈改变需求"
        rejection_task_id = ""
        if args.action == "reject":
            if not active_plan.is_file():
                print("❌ reject 只接受 active Sprint")
                return 1
            try:
                rejection_task_id = _acceptance_task_id(active_plan)
            except (OSError, UnicodeError, ValueError) as exc:
                print(f"❌ 验收回退预检失败: {exc}")
                return 1
        if args.action == "reject" and args.feedback_classification in {
            "requirement-change",
            "new-story",
        }:
            try:
                _story_rejection_update(root.resolve(), active_plan, rejection_reason)
            except (OSError, UnicodeError, ValueError) as exc:
                print(f"❌ 需求反馈回退预检失败: {exc}")
                return 1
        mutation_paths = [root / "USER_STORIES.md", target, outcome_target]
        if active_plan.is_file():
            mutation_paths.append(active_plan)
        snapshot = _snapshot(mutation_paths)
        state_directories = [
            root / ".harness/state/requirements/events",
            root / ".harness/state/requirements/confirmations",
            root / ".harness/state/requirements/feedback",
        ]
        state_before = {
            directory: set(directory.glob("*.json")) if directory.exists() else set() for directory in state_directories
        }
        feedback_receipt: dict = {}
        try:
            if args.feedback_classification:
                feedback_receipt = record_feedback(
                    root.resolve(),
                    args.sprint,
                    args.feedback_classification,
                    args.summary,
                    args.by,
                    input_path=args.story_input.resolve() if args.story_input else None,
                    disposition=args.feedback_disposition,
                )
            if args.action == "reject" and active_plan.is_file():
                if feedback_receipt.get("responsible_scope") == "story":
                    updated_plan = _story_rejection_update(root.resolve(), active_plan, rejection_reason)
                else:
                    updated_plan, _ = rollback(
                        active_plan.read_text(encoding="utf-8"),
                        rejection_task_id,
                        "code",
                        args.summary or "Boss 走查驳回",
                    )
            else:
                updated_plan = None
            contract_plan = active_plan if args.action == "reject" else plan
            contract_sha = (
                planning_contract_digest(contract_plan)
                if contract_plan is not None
                and sprint_planning_contract(contract_plan)["planning_contract_version"] is not None
                else ""
            )
            signoff = {
                "sprint": args.sprint,
                "decision": "approved" if args.action == "approve" else "rejected",
                "confirmed_by": args.by,
                "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "commit_sha": sha,
                "source": "ask_user",
                "summary": args.summary,
                "planning_contract_sha256": contract_sha,
            }
            if args.action == "approve" and plan is not None:
                signoff["outcome"] = {
                    "planning_contract_sha256": contract_sha,
                    "result": "passed",
                    "evidence": outcome_receipt["evidence"],
                }
            if feedback_receipt:
                signoff["feedback"] = feedback_receipt
            writes: list[tuple[Path, dict]] = [(target, signoff)]
            if args.action == "approve" and plan is not None:
                writes.append((outcome_target, outcome_receipt))
            write_yaml_bundle(writes)
            if updated_plan is not None:
                StateStore(active_plan.parent).write_text(active_plan.name, updated_plan)
        except (OSError, UnicodeError, ValueError) as exc:
            _restore(snapshot)
            for directory, previous in state_before.items():
                for path in set(directory.glob("*.json")) - previous if directory.exists() else set():
                    path.unlink(missing_ok=True)
            print(f"❌ 验收记录事务已回退: {exc}")
            return 1
        print(f"✅ 审批记录已写入: {target}")
        return 0
    for error in errors:
        print(f"❌ {error}")
    print("PASS" if not errors else "BLOCKED")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reopen the responsible Sprint stage from a recorded Review scope conflict."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.sprint_context import (
    complete_task_reopen_transfer,
    task_reopen_transfer_errors,
)
from mai_harness.runtime.application.task_evidence import load_current_attempt
from mai_harness.runtime.commands.task_rollback import reopen_scope
from mai_harness.runtime.domain.scope_routing import RESPONSIBLE_SCOPES, resolve_scope_route
from mai_harness.runtime.domain.sprint_context import (
    planning_contract_digest,
    sprint_header,
    sprint_planning_contract,
    sprint_structure_digest,
    table_rows,
)
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import load_yaml


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sprint_path", type=Path)
    parser.add_argument("--from-task-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--transfer-sprint", help="完成 transfer-required 的目标 completed Sprint ID")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    sprint = args.sprint_path.resolve()
    expected_parent = root / "docs/exec-plans/active"
    if not sprint.is_file() or sprint.parent != expected_parent.resolve():
        parser.error("task-reopen 只接受当前工程 docs/exec-plans/active/ 中的 Sprint")
    paths = HarnessPaths.detect(project=root)
    rules_path = paths.rules / "task-rules.yml"
    rules = load_yaml(rules_path)
    rows = table_rows(sprint.read_text(encoding="utf-8"))
    source = next((row for row in rows if row.get("id") == args.from_task_id), None)
    if source is None:
        parser.error(f"Review 来源任务未登记在 Sprint: {args.from_task_id}")
    task_type = source.get("类型") or source.get("type") or ""
    task = (rules.get("tasks") or {}).get(task_type)
    if not isinstance(task, dict):
        parser.error(f"Review 来源任务类型未登记: {task_type}")
    try:
        state = load_current_attempt(root, sprint, rules_path, args.from_task_id, task_type, task)
    except ValueError as exc:
        parser.error(str(exc))
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if review.get("decision") not in {"fail", "incomplete"}:
        parser.error("task-reopen 只接受当前 attempt 的 FAIL/INCOMPLETE Review")
    report = root / str(review.get("report", ""))
    if not report.is_file() or _digest(report) != review.get("report_sha256"):
        parser.error("当前 Review report 缺失或已变化")
    try:
        document = json.loads(report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        parser.error(f"Review report 无法读取: {exc}")
    conflicts = [
        item
        for item in document.get("findings", [])
        if isinstance(item, dict) and item.get("finding_type") == "scope_conflict"
    ]
    scopes = {str(item.get("responsible_scope", "")) for item in conflicts}
    precedence = {name: index for index, name in enumerate(RESPONSIBLE_SCOPES)}
    if not conflicts or not scopes or not scopes <= set(precedence):
        parser.error("Review 必须包含声明 responsible_scope 的 scope_conflict finding")
    responsible_scope = min(scopes, key=precedence.__getitem__)
    sprint_type = sprint_header(sprint).get("sprint_type", "")
    try:
        route = resolve_scope_route(rules, sprint_type, responsible_scope)
    except ValueError as exc:
        parser.error(str(exc))
    owner_types = set(route.get("owner_tasks", ()))
    transfer_to = route.get("transfer_to")
    if args.transfer_sprint and not isinstance(transfer_to, str):
        parser.error("当前责任域在本 Sprint 内回退，不接受 --transfer-sprint")
    owner_present = any((row.get("类型") or row.get("type")) in owner_types for row in rows)
    requirements_sha256 = (state.get("context") or {}).get("requirements_sha256")
    if responsible_scope == "story" and owner_types and not requirements_sha256:
        parser.error("story 责任域只适用于绑定 USER_STORIES 的需求型 Sprint")
    name = f"{sprint.stem}--{args.from_task_id}--{state['run_id'][:12]}.json"
    receipt_store = StateStore(root / ".harness/state/task-reopens")
    try:
        existing = receipt_store.read_json(name, None)
    except ValueError as exc:
        parser.error(str(exc))
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or existing.get("review_report_sha256") != review.get("report_sha256")
            or existing.get("responsible_scope") != responsible_scope
        ):
            parser.error("同一 task attempt 已存在不一致的 task-reopen receipt")
        if args.transfer_sprint:
            if existing.get("transfer_to") != transfer_to or existing.get("status") not in {
                "transfer-required",
                "transferred",
            }:
                parser.error("现有 task-reopen receipt 不是可完成的责任域转移")
            if existing.get("status") == "transferred":
                if existing.get("transfer_sprint") != args.transfer_sprint:
                    parser.error("责任域转移已绑定其他 completed Sprint")
                if transfer_errors := task_reopen_transfer_errors(root, existing, rules):
                    parser.error("责任域转移证据已失效:\n- " + "\n- ".join(transfer_errors))
                result = existing
                idempotent = True
            else:
                try:
                    result = complete_task_reopen_transfer(root, existing, args.transfer_sprint, rules)
                except ValueError as exc:
                    parser.error(str(exc))
                receipt_store.write_json(name, result)
                idempotent = False
            print(
                json.dumps(
                    {
                        "ok": True,
                        "receipt": str(receipt_store.path(name)),
                        "idempotent": idempotent,
                        **result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if existing.get("status") == "transferred" and (
            transfer_errors := task_reopen_transfer_errors(root, existing, rules)
        ):
            parser.error("责任域转移证据已失效:\n- " + "\n- ".join(transfer_errors))
        print(
            json.dumps(
                {"ok": True, "receipt": str(receipt_store.path(name)), "idempotent": True, **existing},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    structure_before = sprint_structure_digest(sprint)
    base_receipt = {
        "schema_version": 1,
        "sprint": sprint.stem,
        "from_task_id": args.from_task_id,
        "from_run_id": state["run_id"],
        "review_report": review["report"],
        "review_report_sha256": review["report_sha256"],
        "planning_contract_sha256": planning_contract_digest(sprint),
        "sprint_structure_sha256_before": structure_before,
        "requirements_sha256_before": requirements_sha256,
        "source_stories": (
            sprint_planning_contract(sprint).get("source_stories") if responsible_scope == "story" else []
        ),
        "responsible_scope": responsible_scope,
        "declared_scopes": sorted(scopes),
        "reason": args.reason,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if isinstance(transfer_to, str):
        receipt = {
            **base_receipt,
            "expected_owner_types": [],
            "affected_task_ids": [],
            "transfer_to": transfer_to,
            "status": "transfer-required",
            "requires_transfer": True,
            "requires_requirements_feedback": False,
            "requires_structure_amend": False,
            "requires_amend": False,
        }
        receipt_path = receipt_store.write_json(name, receipt)
        if args.transfer_sprint:
            try:
                receipt = complete_task_reopen_transfer(root, receipt, args.transfer_sprint, rules)
            except ValueError as exc:
                parser.error(str(exc))
            receipt_path = receipt_store.write_json(name, receipt)
        print(
            json.dumps(
                {
                    "ok": True,
                    "receipt": str(receipt_path),
                    "next_action": (
                        "目标 Sprint 已验证，当前 Sprint 可继续创建新 attempt"
                        if receipt["status"] == "transferred"
                        else f"创建并完成新的 {transfer_to}，在目标计划声明 scope_transfer_from 后"
                        f"用 --transfer-sprint <Sprint ID> 闭环；当前 Sprint 保持阻塞"
                    ),
                    **receipt,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    stages = (rules.get("sprint_type_sequences") or {}).get(sprint_type) or []
    try:
        updated, affected = reopen_scope(
            sprint.read_text(encoding="utf-8"),
            stages,
            args.from_task_id,
            responsible_scope,
            owner_types,
            args.reason,
        )
    except ValueError as exc:
        parser.error(str(exc))
    sprint.write_text(updated, encoding="utf-8")
    requires_feedback = responsible_scope == "story"
    requires_structure_amend = not owner_present
    requires_amend = requires_feedback or requires_structure_amend
    receipt = {
        **base_receipt,
        "expected_owner_types": sorted(owner_types),
        "affected_task_ids": affected,
        "status": (
            "awaiting-requirements-feedback"
            if requires_feedback
            else "awaiting-scope-amend"
            if requires_structure_amend
            else "reopened"
        ),
        "requires_requirements_feedback": requires_feedback,
        "requires_structure_amend": requires_structure_amend,
        "requires_amend": requires_amend,
        "requires_transfer": False,
    }
    receipt_path = receipt_store.write_json(name, receipt)
    print(json.dumps({"ok": True, "receipt": str(receipt_path), **receipt}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

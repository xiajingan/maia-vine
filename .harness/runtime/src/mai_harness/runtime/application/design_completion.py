"""Close a Ready design only after its implementation and architecture conformance were reviewed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.design_publication import publication_errors, registry_revision
from mai_harness.runtime.application.task_evidence import current_attempt_state_path, validate_attempt
from mai_harness.runtime.domain.design_policy import (
    ARCHITECTURE_STATUS_LINE,
    HUMAN_DESIGN_TASKS,
    TECHNICAL_TASKS,
    file_digest,
    policy_version,
    with_document_status,
)
from mai_harness.runtime.domain.document_registry import REGISTRY_COLUMNS, REGISTRY_TASKS, registry_link_target
from mai_harness.runtime.domain.sprint_context import (
    sprint_planning_contract,
    table_rows,
    task_dependency_graph,
    transitive_task_dependencies,
)
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def completed_design_errors(root: Path, plan: Path) -> list[str]:
    if policy_version(sprint_planning_contract(plan)) != 2:
        return []
    errors = []
    architecture = root / "ARCHITECTURE.md"
    if architecture.is_file() and ARCHITECTURE_STATUS_LINE.findall(
        architecture.read_text(encoding="utf-8").partition("\n# ")[0] + "\n"
    ) == ["pending"]:
        errors.append("架构仍为待实现；归档前须以覆盖全部实现和技术方案的 Review 执行 --complete-from")
    for row in table_rows(plan.read_text(encoding="utf-8")):
        if (row.get("类型") or row.get("type")) not in HUMAN_DESIGN_TASKS:
            continue
        path = current_attempt_state_path(root, plan, row["id"])
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not state.get("completion"):
                errors.append(f"设计 {row['id']} 尚未完成实现与架构核对；先执行 task-approve --complete-from")
            errors.extend(publication_errors(root, state))
            errors.extend(completion_errors(root, plan, state))
        except (OSError, ValueError) as exc:
            errors.append(f"设计 {row['id']} 完成记录缺失或损坏: {exc}")
    return errors


def completion_errors(root: Path, plan: Path, state: dict[str, Any]) -> list[str]:
    """Done is current only while every implementation/review input remains unchanged.

    Keep this out of Ready admission: implementation rework may consume the same
    approved design while its previous Done evidence is no longer current.
    """
    receipt = state.get("completion") or {}
    errors = []
    report = root / str(receipt.get("report", ""))
    if not report.is_file() or file_digest(report) != receipt.get("report_sha256"):
        errors.append("Done 核对报告缺失或已变化；重新提交实现核对")
    if receipt.get("architecture_sha256") != file_digest(root / "ARCHITECTURE.md"):
        errors.append("Done 核对未绑定当前架构；重新提交实现核对")
    sources = receipt.get("sources")
    if not isinstance(sources, dict) or not sources:
        return errors + ["Done 缺少当前实现轮次记录；重新提交实现核对"]
    rows = {row["id"]: row for row in table_rows(plan.read_text(encoding="utf-8"))}
    graph, graph_errors = task_dependency_graph(list(rows.values()))
    expected_sources = {
        row["id"]
        for row in rows.values()
        if (row.get("类型") or row.get("type")) in {"code", "library-code"}
        and state["task_id"] in transitive_task_dependencies(graph, row["id"])
    } | {receipt.get("task_id")}
    if graph_errors or set(sources) != expected_sources:
        errors.append("Done 的实现范围已变化；重新提交全部相关实现核对")
    for task_id, expected in sources.items():
        try:
            current = json.loads(current_attempt_state_path(root, plan, task_id).read_text(encoding="utf-8"))
            review = current.get("review") or {}
            if (
                rows.get(task_id, {}).get("状态", rows.get(task_id, {}).get("status")) not in {"done", "完成", "通过"}
                or {"run_id": current.get("run_id"), "review": review} != expected
                or file_digest(root / review["report"]) != review["report_sha256"]
                or any(file_digest(root / item["path"]) != item["sha256"] for item in review["artifacts"])
            ):
                errors.append(f"Done 的实现/核对依据已变化: {task_id}；重新提交实现核对")
        except (OSError, ValueError, KeyError, TypeError):
            errors.append(f"Done 的实现/核对依据缺失: {task_id}")
    return errors


def complete_design(
    root: Path, plan: Path, rules_path: Path, rules: dict[str, Any], state: dict[str, Any], source_id: str
) -> dict[str, Any]:
    store = StateStore(root / ".harness/state/document-registry/transactions")
    with store.lock("publish"):
        return _complete_design(root, plan, rules_path, rules, state, source_id, store)


def _complete_design(
    root: Path,
    plan: Path,
    rules_path: Path,
    rules: dict[str, Any],
    state: dict[str, Any],
    source_id: str,
    store: StateStore,
) -> dict[str, Any]:
    state_path = current_attempt_state_path(root, plan, state["task_id"])
    if json.loads(state_path.read_text(encoding="utf-8")) != state:
        raise ValueError("完成核对前任务轮次已变化")
    if errors := publication_errors(root, state):
        raise ValueError("\n".join(errors))
    rows = table_rows(plan.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in rows}
    graph, errors = task_dependency_graph(rows)
    if errors or source_id not in by_id or state["task_id"] not in transitive_task_dependencies(graph, source_id):
        raise ValueError("完成证据必须来自依赖当前设计的已评审实现/质量任务")
    consumers = [
        row
        for row in rows
        if (row.get("类型") or row.get("type")) in {"code", "library-code"}
        and state["task_id"] in transitive_task_dependencies(graph, row["id"])
    ]
    if not consumers:
        raise ValueError("当前设计尚无实际实现任务，不能从 Ready 升为 Done")
    sources = {row["id"] for row in consumers} | {source_id}
    for task_id in sources:
        row = by_id[task_id]
        task_type = row.get("类型") or row.get("type")
        if (row.get("状态") or row.get("status")) not in {"done", "完成", "通过"}:
            raise ValueError(f"实现/核对任务尚未完成: {task_id}")
        errors = validate_attempt(root, plan, rules_path, task_id, task_type, rules["tasks"][task_type], upstream=True)
        if errors:
            raise ValueError(f"实现/核对任务 {task_id} 无效: " + "; ".join(errors))
    source_path = current_attempt_state_path(root, plan, source_id)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    report = source["review"]
    document = json.loads((root / report["report"]).read_text(encoding="utf-8"))
    conformance = document.get("design_conformance")
    if (
        not isinstance(conformance, dict)
        or not isinstance(conformance.get("design_run_ids"), list)
        or not all(isinstance(run, str) for run in conformance["design_run_ids"])
        or state["run_id"] not in conformance["design_run_ids"]
    ):
        raise ValueError("完成 Review 必须明确签认当前设计 run_id 的 design_conformance")
    if conformance.get("architecture_sha256") != file_digest(root / "ARCHITECTURE.md"):
        raise ValueError("完成核对未绑定当前唯一架构")
    observations = conformance.get("observations")
    bound = {item["path"] for item in report["artifacts"]}
    if (
        not isinstance(observations, list)
        or not observations
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("object"), str)
            or not item["object"].strip()
            or not isinstance(item.get("implementation"), str)
            or not isinstance(item.get("verification"), str)
            or item.get("implementation") not in bound
            or item.get("verification") not in bound
            for item in observations
        )
    ):
        raise ValueError("架构核对必须包含具体对象，以及当前 Review 实际绑定的实现和验证文件")
    receipt = {
        "task_id": source_id,
        "run_id": source["run_id"],
        "report": report["report"],
        "report_sha256": report["report_sha256"],
        "design_run_id": state["run_id"],
        "architecture_sha256": conformance["architecture_sha256"],
        "sources": {
            task_id: {"run_id": current["run_id"], "review": current["review"]}
            for task_id in sorted(sources)
            for current in [json.loads(current_attempt_state_path(root, plan, task_id).read_text(encoding="utf-8"))]
        },
    }
    if state.get("completion") == receipt:
        return state
    revision = registry_revision(root, state)
    index = root / revision["registry"]
    lines = index.read_text(encoding="utf-8").splitlines()
    directory = REGISTRY_TASKS[state["task_type"]][0]
    status_column = REGISTRY_COLUMNS[directory].index("状态")
    ids = {row["Entry ID"] for row in revision["rows"]}
    for number, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in ids:
            cells[status_column] = "done"
            lines[number] = "| " + " | ".join(cells) + " |"
    updated = {**state, "completion": receipt}
    state_path = current_attempt_state_path(root, plan, state["task_id"])
    changes = {index: "\n".join(lines) + "\n", state_path: json.dumps(updated, ensure_ascii=False, indent=2) + "\n"}
    for row in revision["rows"]:
        path = index.parent / registry_link_target(row["文件"])
        changes[path] = with_document_status(path.read_text(encoding="utf-8"), "done")
    all_code = {row["id"] for row in rows if (row.get("类型") or row.get("type")) in {"code", "library-code"}}
    # The conformance review covers this design's validated implementation set,
    # including parallel consumers that need not depend on one another.
    covered = transitive_task_dependencies(graph, source_id) | sources
    technical_runs = set()
    if all_code <= covered:
        for row in rows:
            task_type = row.get("类型") or row.get("type")
            if task_type in TECHNICAL_TASKS:
                technical_runs.add(
                    json.loads(current_attempt_state_path(root, plan, row["id"]).read_text(encoding="utf-8"))["run_id"]
                )
        for task_id in all_code - sources:
            row = by_id[task_id]
            task_type = row.get("类型") or row.get("type")
            if (row.get("状态") or row.get("status")) not in {"done", "完成", "通过"} or validate_attempt(
                root, plan, rules_path, task_id, task_type, rules["tasks"][task_type], upstream=True
            ):
                raise ValueError(f"整体架构完成核对仍有未完成或无效的实现: {task_id}")
    if all_code <= covered and technical_runs and technical_runs <= set(conformance["design_run_ids"]):
        architecture = root / "ARCHITECTURE.md"
        changes[architecture] = ARCHITECTURE_STATUS_LINE.sub(
            "architecture_implementation_status: done\n", architecture.read_text(encoding="utf-8"), count=1
        )
    files = {
        path.relative_to(root).as_posix(): {"before": path.read_text(encoding="utf-8"), "after": value}
        for path, value in changes.items()
    }
    transaction = {"status": "prepared", "files": files}
    name = state["run_id"] + "-complete.json"
    store.write_json(name, transaction)
    try:
        for relative, versions in files.items():
            StateStore(root).write_text(relative, versions["after"])
        transaction["status"] = "committed"
        store.write_json(name, transaction)
    except Exception:
        for relative, versions in files.items():
            StateStore(root).write_text(relative, versions["before"])
        transaction["status"] = "rolled-back"
        store.write_json(name, transaction)
        raise
    return updated

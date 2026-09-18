"""Publish one reviewed and human-approved design revision with its architecture delta."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.human_approval import require_approval
from mai_harness.runtime.domain.design_policy import document_digest, document_status, file_digest
from mai_harness.runtime.domain.document_registry import (
    REGISTRY_TASKS,
    _parse_rows,
    registry_publication_changes,
    task_registry_publication,
    validate_registry,
    validate_task_registry,
)
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def registry_revision(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    directory = REGISTRY_TASKS[state["task_type"]][0]
    index = root / "docs" / directory / "index.md"
    rows, errors = _parse_rows(index, directory)
    if errors:
        raise ValueError("\n".join(errors))
    return {
        "registry": index.relative_to(root).as_posix(),
        "rows": [
            row.raw
            for row in rows
            if row.sprint == state["sprint"] and row.task_id == state["task_id"] and row.run_id == state["run_id"]
        ],
    }


def prototype_files(root: Path, roots: list[str]) -> list[Path]:
    """Explicit, self-contained resource directories avoid guessing dependency syntax."""
    found = set()
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise ValueError("prototype_roots 必须是工程内原型资源目录列表")
    for relative in roots:
        path = (root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_dir():
            raise ValueError(f"原型资源目录缺失或越界: {relative}")
        if path == root.resolve() or path.name == ".harness":
            raise ValueError("原型必须使用独立资源目录")
        for item in path.rglob("*"):
            if item.is_symlink():
                raise ValueError(f"原型资源目录不接受符号链接: {item}")
            if item.is_file():
                found.add(item.relative_to(root.resolve()))
    return sorted(found)


def artifact_bundle(
    root: Path, artifacts: list[Path], *, ui: bool = False, prototype_roots: list[str] | None = None
) -> list[dict[str, str]]:
    roots = prototype_roots or []
    resources = prototype_files(root, roots)
    if ui and not roots:
        raise ValueError("UI Review 必须声明 prototype_roots，绑定完整原型/设计稿资源目录")
    found = {}
    for relative in [*artifacts, *resources]:
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"审核产物或原型依赖缺失/越界: {relative}")
        found[path.relative_to(root.resolve()).as_posix()] = file_digest(path)
    if ui and not any(path.suffix in {".html", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".pdf"} for path in resources):
        raise ValueError("UI 资源目录必须包含原型/设计稿或未变化的现状证据")
    return [{"path": path, "sha256": sha} for path, sha in sorted(found.items())]


def current_bundle_errors(
    root: Path, bundle: list[dict[str, str]], prototype_roots: list[str] | None = None
) -> list[str]:
    errors = []
    for item in bundle:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or file_digest(path) != item["sha256"]:
            errors.append(f"已审核产物缺失或版本变化: {item['path']}")
    try:
        resources = prototype_files(root, prototype_roots or [])
        recorded = {item["path"] for item in bundle}
        if any(path.as_posix() not in recorded for path in resources):
            errors.append("原型资源目录新增文件，必须重新审核完整资源包")
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def require_stable_publications(root: Path) -> None:
    """Readers must not adopt architecture/documents from a partially written transaction."""
    store = StateStore(root / ".harness/state/document-registry/transactions")
    for path in store.root.glob("*.json"):
        if store.read_json(path.name, {}).get("status") == "prepared":
            raise ValueError(f"设计发布尚未完成，先用 task-approve 恢复该轮次，再继续依赖任务: {path.stem}")


def publication_errors(root: Path, state: dict[str, Any]) -> list[str]:
    """The committed receipt, not editable document labels, authorizes downstream use."""
    try:
        return _publication_errors(root, state)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"设计发布依据缺失或损坏，禁止下游准入: {exc}"]


def _publication_errors(root: Path, state: dict[str, Any]) -> list[str]:
    approval = state.get("approval")
    if not isinstance(approval, dict):
        return ["缺少当前设计版本的人工审核请求"]
    try:
        require_approval(root, approval)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    publication = state.get("publication")
    if not isinstance(publication, dict):
        return ["人工已确认，但当前设计尚未完成一致发布"]
    store = StateStore(root / ".harness/state/document-registry/transactions")
    transaction = store.read_json(state["run_id"] + ".json", {})
    if transaction.get("status") != "committed":
        return ["设计发布未完成或待恢复，禁止下游准入"]
    path = root / publication.get("path", "")
    if not path.is_file() or file_digest(path) != publication.get("sha256"):
        return ["设计发布凭证缺失或已变化"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("approval") != approval or payload.get("run_id") != state.get("run_id"):
        return ["设计发布未绑定当前人工批准与任务版本"]
    errors = current_bundle_errors(root, approval["payload"]["artifacts"], approval["payload"].get("prototype_roots"))
    live = registry_revision(root, state)
    for row in live["rows"]:
        if row["状态"] not in {"ready", "done"}:
            errors.append("当前方案已失效或尚未发布为 Ready")
        if state.get("completion") and row["状态"] != "done":
            errors.append("实现完成记录与文档索引 Done 状态不一致")
        relative = next((item["file"] for item in payload["rows"] if item["entry_id"] == row["Entry ID"]), None)
        if relative and document_status((root / relative).read_text(encoding="utf-8")) != row["状态"]:
            errors.append("当前索引与文档状态不一致")
        if row["状态"] == "done":
            completion = state.get("completion") or {}
            completed = store.read_json(state["run_id"] + "-complete.json", {})
            if (
                completed.get("status") != "committed"
                or completion.get("design_run_id") != state["run_id"]
            ):
                errors.append("文档 Done 缺少实现完成与架构核对证据，手工标记不能替代验证")
    if not live["rows"]:
        errors.append("当前版本在索引中不存在")
    approved_rows = approval["payload"]["registry"]["rows"]

    def normalize(rows):
        return [{key: value for key, value in row.items() if key != "状态"} for row in rows]

    if normalize(live["rows"]) != normalize(approved_rows):
        errors.append("当前索引身份或范围与人工批准不一致")
    return errors


def approved_architecture_path(root: Path, before: str, after: str, *, sprint: str, task_id: str) -> bool:
    """Recognize already committed design outputs without treating arbitrary newer code as authority."""
    if before == after:
        return True
    edges: dict[str, set[str]] = {}
    for path in (root / ".harness/state/document-registry/publications").glob("*.json"):
        try:
            publication = json.loads(path.read_text(encoding="utf-8"))
            delta = publication.get("architecture_change")
            if publication.get("schema_version") != 2 or not delta:
                continue
            if publication.get("sprint") == sprint and task_id in delta.get("affected_design_task_ids", []):
                continue
            transaction = StateStore(root / ".harness/state/document-registry/transactions").read_json(
                publication["run_id"] + ".json", {}
            )
            require_approval(root, publication["approval"])
            if (
                transaction.get("status") != "committed"
                or publication["approval"]["payload"].get("architecture_change") != delta
            ):
                continue
            edges.setdefault(delta["before_sha256"], set()).add(delta["after_sha256"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    pending, visited = [before], set()
    while pending:
        current = pending.pop()
        if current == after:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(edges.get(current, ()))
    return False


def recover_publication(root: Path, run_id: str) -> None:
    """Restore an interrupted write set before reloading the task's architecture inputs."""
    store = StateStore(root / ".harness/state/document-registry/transactions")
    with store.lock("publish"):
        for name in (run_id + ".json", run_id + "-complete.json"):
            transaction = store.read_json(name, {})
            if transaction.get("status") != "prepared":
                continue
            for relative, versions in transaction["files"].items():
                path = StateStore(root).path(relative)
                current = path.read_text(encoding="utf-8") if path.exists() else None
                if current not in (versions["before"], versions["after"]):
                    raise ValueError(f"发布恢复发现外部修改，需先解决冲突: {relative}")
            for relative, versions in transaction["files"].items():
                if versions["before"] is None:
                    StateStore(root).path(relative).unlink(missing_ok=True)
                else:
                    StateStore(root).write_text(relative, versions["before"])
            transaction["status"] = "rolled-back"
            store.write_json(name, transaction)


def publish_design(root: Path, state_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Journal changes before writing; a crash remains blocked until the same operation resumes."""
    approval = state.get("approval") or {}
    require_approval(root, approval)
    payload = approval["payload"]
    if payload["run_id"] != state["run_id"]:
        raise ValueError("人工确认不属于当前 attempt")
    report = root / payload["report"]
    if (
        not report.is_file()
        or file_digest(report) != payload["report_sha256"]
        or state.get("review", {}).get("decision") != "pass"
    ):
        raise ValueError("已审核 Review 证据缺失或变化，不能发布")
    store = StateStore(root / ".harness/state/document-registry/transactions")
    with store.lock("publish"):
        require_stable_publications(root)
        if json.loads(state_path.read_text(encoding="utf-8")) != state:
            raise ValueError("任务轮次在发布前已变化，拒绝使用旧状态")
        name = state["run_id"] + ".json"
        transaction = store.read_json(name, {})
        if transaction.get("status") == "committed":
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if errors := publication_errors(root, current):
                raise ValueError("\n".join(errors))
            return current
        if errors := current_bundle_errors(root, payload["artifacts"], payload.get("prototype_roots")):
            raise ValueError("\n".join(errors))
        if registry_revision(root, state) != payload["registry"]:
            raise ValueError("Review 后当前索引范围已变化，必须重新审核")
        paths = [Path(item["path"]) for item in payload["artifacts"]]
        paths.append(Path(payload["registry"]["registry"]))
        errors = validate_task_registry(
            root,
            state["sprint"],
            state["task_id"],
            state["run_id"],
            state["task_type"],
            paths,
            payload["source_stories"],
            payload["requirement_mode"],
            policy=2,
        )
        if errors:
            raise ValueError("\n".join(errors))
        architecture = payload.get("architecture_change")
        if architecture and file_digest(root / "ARCHITECTURE.md") != architecture["before_sha256"]:
            raise ValueError("架构基线已变化，必须合并共享修改并重新 Review/人工确认，禁止覆盖")
        publication_relative = f".harness/state/document-registry/publications/{state['sprint']}--{state['task_id']}--{state['run_id']}.json"
        changes = registry_publication_changes(
            root, state["sprint"], state["task_id"], state["run_id"], state["task_type"], policy=2
        )
        if architecture:
            changes[root / "ARCHITECTURE.md"] = (root / architecture["candidate"]).read_text(encoding="utf-8")
        index = root / payload["registry"]["registry"]
        publication = task_registry_publication(
            root, state["sprint"], state["task_id"], state["run_id"], state["task_type"], pending_index=changes[index]
        )
        publication.update({"schema_version": 2, "approval": approval, "architecture_change": architecture})
        publication_text = json.dumps(publication, ensure_ascii=False, indent=2) + "\n"
        changes[root / publication_relative] = publication_text
        updated = {
            **state,
            "publication": {"path": publication_relative, "sha256": document_digest(publication_text.encode())},
            "human_status": "approved",
        }
        changes[state_path] = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        files = {
            path.relative_to(root).as_posix(): {
                "before": path.read_text(encoding="utf-8") if path.exists() else None,
                "after": content,
            }
            for path, content in changes.items()
        }
        transaction = {"status": "prepared", "files": files, "approval": approval["request_sha256"]}
        store.write_json(name, transaction)
        try:
            for relative, versions in files.items():
                StateStore(root).write_text(relative, versions["after"])
            if errors := validate_registry(root, REGISTRY_TASKS[state["task_type"]][0]):
                raise ValueError("\n".join(errors))
            transaction["status"] = "committed"
            store.write_json(name, transaction)
        except Exception:
            for relative, versions in files.items():
                if versions["before"] is None:
                    (root / relative).unlink(missing_ok=True)
                else:
                    StateStore(root).write_text(relative, versions["before"])
            transaction["status"] = "rolled-back"
            store.write_json(name, transaction)
            raise
        return updated

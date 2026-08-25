"""Control-plane coordination, release composition, and integration state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mai_harness.runtime.application.collaboration import dispatch_assignment, target_assignment_service
from mai_harness.runtime.application.dependency_session import validate_session
from mai_harness.runtime.domain.dependency_graph import dependency_order
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import resolve_managed_project, resolve_project_relative
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.manifest import (
    digest,
    load_manifest,
    now,
    validate_delivery,
    validate_release,
    write_manifest,
)
from mai_harness.runtime.infrastructure.utils import load_yaml


def load_managed_projects(path: Path) -> dict[str, dict[str, Any]]:
    data = load_yaml(path)
    if data.get("version") != 1 or not isinstance(data.get("projects"), dict):
        raise ValueError("managed-projects.yml: version=1 且 projects 必须为映射")
    projects = data["projects"]
    dependency_order(projects)
    for project_id, entry in projects.items():
        for field in (
            "repository",
            "owner",
            "project_path",
            "assignment_inbox",
            "assignment_responses",
            "deliveries_dir",
        ):
            if not entry.get(field):
                raise ValueError(f"projects.{project_id}.{field}: 必填")
        project_path = Path(entry["project_path"])
        if project_path.is_absolute() or str(project_path) in {"", "."}:
            raise ValueError(f"projects.{project_id}.project_path: 必须是相对 Control 的工程路径")
        for field in ("assignment_inbox", "assignment_responses", "deliveries_dir"):
            value = Path(entry[field])
            if value.is_absolute() or ".." in value.parts:
                raise ValueError(f"projects.{project_id}.{field}: 必须是 Managed 工程内相对路径")
    return projects


class ControlAssignmentService:
    def __init__(self, state_root: Path, projects: dict[str, dict[str, Any]], source_project_id: str = "") -> None:
        self.root = state_root / "assignments"
        self.projects = projects
        self.source_project_id = source_project_id

    def dispatch(self, assignment: dict[str, Any], control_root: Path) -> dict[str, Any]:
        project_id = assignment.get("target_project_id")
        if project_id not in self.projects:
            raise ValueError(f"Assignment 分发校验失败:\n- target_project_id: 未登记工程 {project_id}")
        project = self.projects[project_id]
        managed_root = resolve_managed_project(
            control_root, project["project_path"], f"projects.{project_id}.project_path"
        )
        registered_inbox = resolve_project_relative(
            managed_root, project["assignment_inbox"], f"projects.{project_id}.assignment_inbox"
        )
        target_service = target_assignment_service(managed_root)
        if target_service.inbox != registered_inbox:
            raise ValueError(f"Managed Assignment inbox 与 Control 登记不一致: {project_id}")
        target = registered_inbox / f"{assignment['assignment_id']}.json"
        receipt = managed_root / ".harness/state/assignments/idempotency" / f"{assignment['idempotency_key']}.json"
        target_existed = target.exists()
        receipt_existed = receipt.exists()
        dispatched = dispatch_assignment(self.source_project_id, managed_root, assignment)
        return {
            "duplicate": receipt_existed,
            "restored": receipt_existed and not target_existed,
            "receipt": receipt,
            "target": dispatched,
        }

    def status(self, control_root: Path) -> list[dict[str, Any]]:
        statuses = []
        global_delivery_ids: dict[str, list[tuple[str, Path]]] = {}
        for project_id, project in sorted(self.projects.items()):
            managed_root = resolve_managed_project(
                control_root, project["project_path"], f"projects.{project_id}.project_path"
            )
            deliveries_dir = resolve_project_relative(
                managed_root, project["deliveries_dir"], f"projects.{project_id}.deliveries_dir"
            )
            for path in sorted(deliveries_dir.glob("*.json")) if deliveries_dir.exists() else []:
                try:
                    item = load_manifest(path)
                except (OSError, ValueError):
                    continue
                delivery_id = str(item.get("delivery_id", ""))
                if delivery_id:
                    global_delivery_ids.setdefault(delivery_id, []).append((project_id, path))
        for project_id, project in sorted(self.projects.items()):
            managed_root = resolve_managed_project(
                control_root, project["project_path"], f"projects.{project_id}.project_path"
            )
            deliveries_dir = resolve_project_relative(
                managed_root, project["deliveries_dir"], f"projects.{project_id}.deliveries_dir"
            )
            responses_dir = resolve_project_relative(
                managed_root, project["assignment_responses"], f"projects.{project_id}.assignment_responses"
            )
            deliveries = sorted(path.name for path in deliveries_dir.glob("*.json")) if deliveries_dir.exists() else []
            responses = sorted(path.name for path in responses_dir.glob("*.json")) if responses_dir.exists() else []
            assignments = target_assignment_service(managed_root).pending()
            known_assignments = {item["assignment_id"] for item in assignments}
            project_errors: list[str] = []
            invalid_documents: list[dict[str, Any]] = []
            for kind, directory in (("Response", responses_dir), ("Delivery", deliveries_dir)):
                for path in sorted(directory.glob("*.json")) if directory.exists() else []:
                    try:
                        item = load_manifest(path)
                    except (OSError, ValueError) as exc:
                        message = f"{kind} JSON 无法解析: {exc}"
                        project_errors.append(f"{path}: {message}")
                        invalid_documents.append({"path": str(path), "errors": [message]})
                        continue
                    assignment_id = item.get("assignment_id")
                    document_errors = []
                    if kind == "Response" and assignment_id not in known_assignments:
                        document_errors.append(f"{kind} 引用了不存在的 Assignment: {assignment_id}")
                    if kind == "Delivery":
                        document_errors.extend(validate_delivery(item))
                        assignment_ids = ([assignment_id] if assignment_id else []) + [
                            binding.get("assignment_id")
                            for binding in ((item.get("satisfies") or {}).get("assignments") or [])
                            if isinstance(binding, dict)
                        ]
                        for referenced in assignment_ids:
                            if referenced not in known_assignments:
                                document_errors.append(f"Delivery 引用了不存在的 Assignment: {referenced}")
                    expected_name = (
                        f"{assignment_id}.json" if kind == "Response" else f"{item.get('delivery_id', '')}.json"
                    )
                    if path.name != expected_name:
                        document_errors.append(f"{kind} 文件名必须是 {expected_name}")
                    if kind == "Delivery" and len(global_delivery_ids.get(str(item.get("delivery_id", "")), [])) != 1:
                        document_errors.append(f"delivery_id 在全部 Managed 工程中不唯一: {item.get('delivery_id')}")
                    if document_errors:
                        project_errors.extend(f"{path}: {error}" for error in document_errors)
                        invalid_documents.append({"path": str(path), kind.lower(): item, "errors": document_errors})
            for assignment in assignments:
                if assignment["status"] == "invalid":
                    project_errors.extend(assignment["errors"])
                    invalid_documents.extend(assignment["invalid_documents"])
            statuses.append(
                {
                    "project_id": project_id,
                    "assignments": assignments,
                    "invalid": bool(project_errors),
                    "errors": project_errors,
                    "invalid_documents": invalid_documents,
                    "responses": responses,
                    "deliveries": deliveries,
                }
            )
        return statuses


def assert_registered_delivery_paths(
    delivery_paths: list[Path], projects: dict[str, dict[str, Any]], control_root: Path
) -> None:
    for path in delivery_paths:
        resolved = path.resolve()
        owner = next(
            (
                project_id
                for project_id, project in projects.items()
                if resolved.parent
                == resolve_project_relative(
                    resolve_managed_project(
                        control_root, project["project_path"], f"projects.{project_id}.project_path"
                    ),
                    project["deliveries_dir"],
                    f"projects.{project_id}.deliveries_dir",
                )
            ),
            None,
        )
        if owner is None:
            raise ValueError(f"Delivery 不在已登记 Managed 发布产出目录: {path}")
        if load_manifest(resolved).get("project_id") != owner:
            raise ValueError(f"Delivery 工程身份与所在目录不一致: {path}（目录所有者 {owner}）")


def assert_registered_delivery_states(
    delivery_paths: list[Path], projects: dict[str, dict[str, Any]], control_root: Path
) -> None:
    """Require each Delivery to be accepted and verified by its owning Managed project."""
    for path in delivery_paths:
        resolved = path.resolve()
        delivery = load_manifest(resolved)
        project_id = delivery.get("project_id")
        project = projects.get(project_id)
        if not project:
            raise ValueError(f"Delivery 声明了未登记工程: {project_id}")
        managed_root = resolve_managed_project(
            control_root, project["project_path"], f"projects.{project_id}.project_path"
        )
        service = target_assignment_service(managed_root)
        assignment_ids = ([delivery.get("assignment_id")] if delivery.get("assignment_id") else []) + [
            item.get("assignment_id")
            for item in ((delivery.get("satisfies") or {}).get("assignments") or [])
            if isinstance(item, dict)
        ]
        for assignment_id in assignment_ids:
            status = service.status(str(assignment_id))
            valid_paths = {Path(item["path"]).resolve() for item in status.get("deliveries", [])}
            if status.get("state") != "delivered" or resolved not in valid_paths:
                raise ValueError(f"Delivery 未通过所属 Managed 的 Assignment/Response/供应链完整门禁: {path}")
        session_bindings = (delivery.get("satisfies") or {}).get("dependency_sessions") or []
        for binding in session_bindings:
            if not isinstance(binding, dict):
                raise ValueError(f"Delivery dependency session 绑定非法: {path}")
            state = StateStore(managed_root / ".harness/state/dependency-sessions/completed").read_json(
                f"{binding.get('session_id', '')}.json"
            )
            if (
                not isinstance(state, dict)
                or validate_session(state)
                or state.get("status") != "completed"
                or state.get("provider_project_id") != project_id
                or state.get("request_digest") != binding.get("request_digest")
                or state.get("delivery", {}).get("manifest_digest") != delivery.get("manifest_digest")
            ):
                raise ValueError(f"Delivery 未通过 coordinated dependency session 完整门禁: {path}")
        if not assignment_ids and not session_bindings:
            raise ValueError(f"Delivery 未绑定 Assignment 或 dependency session: {path}")


def assert_global_delivery_ids(projects: dict[str, dict[str, Any]], control_root: Path) -> None:
    indexed: dict[str, list[Path]] = {}
    for project_id, project in projects.items():
        managed_root = resolve_managed_project(
            control_root, project["project_path"], f"projects.{project_id}.project_path"
        )
        deliveries_dir = resolve_project_relative(
            managed_root, project["deliveries_dir"], f"projects.{project_id}.deliveries_dir"
        )
        for path in sorted(deliveries_dir.glob("*.json")) if deliveries_dir.exists() else []:
            try:
                delivery = load_manifest(path)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Delivery JSON 无法解析: {path}: {exc}") from exc
            delivery_id = str(delivery.get("delivery_id", ""))
            if not delivery_id:
                raise ValueError(f"Delivery 缺少 delivery_id: {path}")
            indexed.setdefault(delivery_id, []).append(path)
    if duplicates := {key: paths for key, paths in indexed.items() if len(paths) != 1}:
        details = "; ".join(f"{key}={[str(path) for path in paths]}" for key, paths in duplicates.items())
        raise ValueError(f"delivery_id 在全部 Managed 工程中不唯一: {details}")


def validate_registered_relationships(
    projects: dict[str, dict[str, Any]], control_root: Path, control_id: str
) -> list[dict[str, str]]:
    """Verify that both project configs identify and point to each other."""
    relationships = []
    for project_id, project in projects.items():
        managed_root = resolve_managed_project(
            control_root, project["project_path"], f"projects.{project_id}.project_path"
        )
        config_path = managed_root / "config/harness.yml"
        if not config_path.exists():
            raise ValueError(f"Managed 配置不存在: {config_path}")
        config = load_yaml(config_path)
        identity = config.get("project", {})
        if identity.get("id") != project_id or identity.get("mode") != "managed":
            raise ValueError(f"Managed 身份不匹配: {project_id}")
        management = config.get("management", {})
        if management.get("control_id") != control_id:
            raise ValueError(f"Managed {project_id} 未登记 Control ID {control_id}")
        control_path = management.get("control_path", "")
        if not control_path or (managed_root / control_path).resolve() != control_root.resolve():
            raise ValueError(f"Managed {project_id} 的 control_path 未指回当前 Control")
        relationships.append({"control_id": control_id, "managed_id": project_id, "managed_path": str(managed_root)})
    return relationships


def validate_contract_compatibility(deliveries: list[dict[str, Any]], projects: dict[str, dict[str, Any]]) -> None:
    provided = {
        contract["name"]: contract["version"]
        for delivery in deliveries
        for contract in delivery.get("contracts", [])
        if contract.get("role") == "provider"
    }
    errors = []
    for project_id, project in projects.items():
        for requirement in project.get("requires_contracts", []):
            actual = provided.get(requirement["name"])
            if actual not in requirement.get("accepted_versions", []):
                errors.append(
                    f"{project_id} requires {requirement['name']} {requirement.get('accepted_versions', [])}, got {actual}"
                )
    if errors:
        raise ValueError("契约不兼容:\n- " + "\n- ".join(errors))


def compose_release(
    release_id: str,
    delivery_paths: list[Path],
    projects: dict[str, dict[str, Any]],
    state_root: Path,
    control_root: Path,
    *,
    environment: str = "test",
) -> Path:
    assert_registered_delivery_paths(delivery_paths, projects, control_root)
    assert_registered_delivery_states(delivery_paths, projects, control_root)
    assert_global_delivery_ids(projects, control_root)
    deliveries = [load_manifest(path) for path in delivery_paths]
    errors = [error for item in deliveries for error in validate_delivery(item)]
    if errors:
        raise ValueError("Delivery 组合校验失败:\n- " + "\n- ".join(errors))
    verification_root = state_root / "control/delivery-verifications"
    for item in deliveries:
        verification = StateStore(verification_root).read_json(
            item["manifest_digest"].removeprefix("sha256:") + ".json"
        )
        expected_artifacts = [
            {
                "type": artifact["type"],
                "ref": artifact["ref"],
                "digest": artifact.get("digest") or "sha256:" + str(artifact.get("sha256", "")).removeprefix("sha256:"),
            }
            for artifact in item["artifacts"]
        ]
        if (
            not isinstance(verification, dict)
            or not verification.get("valid")
            or verification.get("manifest_digest") != item["manifest_digest"]
            or verification.get("artifacts") != expected_artifacts
            or not verification.get("supply_chain")
        ):
            raise ValueError(f"Delivery {item['delivery_id']} 缺少同摘要供应链验证证据")
    indexed = {item["project_id"]: item for item in deliveries}
    required = {name for name, item in projects.items() if item.get("required", True)}
    if missing := required - indexed.keys():
        raise ValueError(f"缺少必需工程交付: {sorted(missing)}")
    validate_contract_compatibility(deliveries, projects)
    order = dependency_order(projects)
    release = {
        "schema_version": 1,
        "release_id": release_id,
        "environment": environment,
        "status": "composed",
        "composed_by": "mai-harness",
        "created_at": now(),
        "dependency_order": order,
        "deliveries": [
            {
                "project_id": name,
                "delivery_id": indexed[name]["delivery_id"],
                "manifest_digest": indexed[name].get("manifest_digest") or digest(indexed[name]),
                "artifacts": indexed[name]["artifacts"],
            }
            for name in order
            if name in indexed
        ],
    }
    release["manifest_digest"] = digest({key: value for key, value in release.items() if key != "status"})
    errors = validate_release(release)
    if errors:
        raise ValueError("Release 校验失败:\n- " + "\n- ".join(errors))
    store = StateStore(state_root / "releases")
    target = store.path(f"{release_id}.json")
    with store.lock(f"{release_id}.compose"):
        if target.exists():
            existing = load_manifest(target)
            comparable = (
                "schema_version",
                "release_id",
                "environment",
                "composed_by",
                "dependency_order",
                "deliveries",
            )
            if all(existing.get(key) == release.get(key) for key in comparable):
                return target
            raise FileExistsError(f"Release {release_id} 已存在且组合不同，禁止原地覆盖")
        write_manifest(target, release)
    return target


def transition_release(path: Path, target: str, *, evidence: list[str] | None = None) -> dict[str, Any]:
    def update(release: Any) -> dict[str, Any]:
        if not isinstance(release, dict):
            raise ValueError("Release Manifest 非法")
        if errors := validate_release(release):
            raise ValueError("Release 校验失败:\n- " + "\n- ".join(errors))
        current = release["status"]
        allowed = {
            "composed": {"test-verifying"},
            "test-verifying": {"test-verified", "failed"},
            "test-verified": {"promoted"},
            "promoted": {"rolled-back"},
            "failed": set(),
            "rolled-back": set(),
        }
        if target not in allowed.get(current, set()):
            raise ValueError(f"非法 Release 状态转换: {current} -> {target}")
        if target in {"test-verified", "promoted", "rolled-back"} and not evidence:
            raise ValueError(f"{target} 状态必须提供证据")
        release["status"] = target
        release["updated_at"] = now()
        release.setdefault("evidence", []).extend(evidence or [])
        return release

    return StateStore(path.parent).update_json(path.name, update)


def promote_stable_release(state_root: Path, manifest_path: Path, release: dict[str, Any]) -> dict[str, Any]:
    """Atomically advance the stable-release ledger after production deployment."""
    entry = {
        "release_id": release["release_id"],
        "manifest": str(manifest_path.resolve()),
        "manifest_digest": release["manifest_digest"],
    }

    def update(current: Any) -> dict[str, Any]:
        ledger = current if isinstance(current, dict) else {}
        if ledger.get("current", {}).get("manifest_digest") == entry["manifest_digest"]:
            return ledger
        return {"current": entry, "previous": ledger.get("current"), "updated_at": now()}

    return StateStore(state_root / "releases").update_json("stable.json", update, default={})


def resolve_rollback_release(state_root: Path, current_manifest: Path) -> Path:
    """Resolve rollback only from the recorded previous stable release."""
    ledger = StateStore(state_root / "releases").read_json("stable.json", {})
    current = ledger.get("current") or {}
    previous = ledger.get("previous") or {}
    if Path(current.get("manifest", "")).resolve() != current_manifest.resolve():
        raise ValueError("回滚目标不是当前稳定 Release")
    if not previous.get("manifest") or previous.get("manifest_digest") == current.get("manifest_digest"):
        raise ValueError("不存在可回退的历史稳定 Release")
    target = Path(previous["manifest"]).resolve()
    release_root = (state_root / "releases").resolve()
    if target.parent != release_root or not target.is_file():
        raise ValueError("历史稳定 Release Manifest 不存在或路径越界")
    release = load_manifest(target)
    if release.get("manifest_digest") != previous.get("manifest_digest") or release.get("status") != "promoted":
        raise ValueError("历史稳定 Release 状态或摘要不匹配")
    return target


def complete_rollback(state_root: Path, rolled_back_manifest: Path | None = None) -> dict[str, Any]:
    """Atomically move the stable ledger back after rollback deployment succeeds."""

    def update(current: Any) -> dict[str, Any]:
        if (
            isinstance(current, dict)
            and not current.get("previous")
            and rolled_back_manifest is not None
            and Path((current.get("current") or {}).get("manifest", "")).resolve() != rolled_back_manifest.resolve()
        ):
            return current
        if not isinstance(current, dict) or not current.get("previous"):
            raise ValueError("稳定版本账本缺少 previous")
        return {"current": current["previous"], "previous": None, "updated_at": now()}

    return StateStore(state_root / "releases").update_json("stable.json", update)


def write_integration_finding(
    state_root: Path, release: dict[str, Any], *, summary: str, suspected_projects: list[str], evidence: list[str]
) -> Path:
    finding_id = f"finding-{release['release_id']}"
    finding = {
        "schema_version": 1,
        "finding_id": finding_id,
        "release_id": release["release_id"],
        "summary": summary,
        "suspected_projects": suspected_projects,
        "evidence": evidence,
        "created_at": now(),
    }
    target = state_root / "findings" / f"{finding_id}.json"
    write_manifest(target, finding)
    return target


def run_test_integration(
    release_path: Path,
    commands: list[list[str]],
    *,
    project_root: Path,
    state_root: Path,
) -> dict[str, Any]:
    if not commands or any(not command for command in commands):
        raise ValueError("Test Integration 至少配置一个非空 argv 命令")
    release = load_manifest(release_path)
    deployments = state_root / "deployments"
    evidence_files = sorted(deployments.glob(f"{release['release_id']}-test-*.json")) if deployments.exists() else []
    valid_deployment = any(
        evidence.get("status") == "applied"
        and evidence.get("environment") == "test"
        and evidence.get("manifest_digest") == release.get("manifest_digest")
        and evidence.get("producer") == "mai-harness:control.test.deploy"
        and all((evidence.get("deployment_identity") or {}).get(key) for key in ("context", "cluster", "namespace"))
        and bool(evidence.get("commands"))
        for evidence in (load_manifest(path) for path in evidence_files)
    )
    if not valid_deployment:
        raise ValueError("Test Integration 必须先完成同一 Release Manifest 的 Test 部署")
    if release["status"] == "composed":
        release = transition_release(release_path, "test-verifying")
    if release["status"] != "test-verifying":
        raise ValueError(f"Test Integration 需要 test-verifying 状态，当前 {release['status']}")
    evidence = []
    for index, command in enumerate(commands, start=1):
        result = execute(CommandSpec.argv_command(command, cwd=project_root))
        evidence.append(f"command:{index}:exit={result.returncode}")
        if not result.ok:
            failed = transition_release(release_path, "failed")
            finding = write_integration_finding(
                state_root,
                failed,
                summary=f"Test Integration command {index} failed",
                suspected_projects=[item["project_id"] for item in failed["deliveries"]],
                evidence=evidence + [result.stderr.strip()[-1000:]],
            )
            return {"status": "failed", "finding": str(finding), "evidence": evidence}
    verified = transition_release(release_path, "test-verified", evidence=evidence)
    return {"status": verified["status"], "evidence": evidence}

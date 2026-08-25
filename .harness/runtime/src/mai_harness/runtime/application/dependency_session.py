"""Coordinated, resumable consumer-to-library dependency sessions."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.collaboration import validate_delivery_publication
from mai_harness.runtime.domain.sprint_context import header_field, table_rows
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import resolve_project_relative
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import command_diagnostics, load_harness_config, resolve_command
from mai_harness.runtime.infrastructure.manifest import (
    digest,
    load_manifest,
    now,
    validate_delivery,
    validate_delivery_verification,
)

ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SPRINT = re.compile(r"^sprint-[0-9]+-[a-z0-9][a-z0-9-]*$")
SESSION_STATUSES = {
    "starting",
    "provider-planning",
    "candidate-built",
    "consumer-verified",
    "completed",
    "failed",
}


def _request(session: dict[str, Any]) -> dict[str, Any]:
    return {
        key: session[key]
        for key in (
            "schema_version",
            "session_id",
            "capability_id",
            "consumer_project_id",
            "consumer_sprint",
            "consumer_task_id",
            "provider_project_id",
            "provider_sprint",
            "package",
            "outcome",
            "acceptance",
        )
    }


def validate_session(session: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if session.get("schema_version") != 1:
        errors.append("schema_version: 必须为 1")
    for field in ("session_id", "capability_id", "consumer_project_id", "provider_project_id"):
        if not isinstance(session.get(field), str) or not ID.fullmatch(session[field]):
            errors.append(f"{field}: 必须是稳定小写 ID")
    if not isinstance(session.get("consumer_task_id"), str) or not REFERENCE.fullmatch(session["consumer_task_id"]):
        errors.append("consumer_task_id: 必须是稳定任务 ID")
    for field in ("consumer_sprint", "provider_sprint"):
        if not isinstance(session.get(field), str) or not SPRINT.fullmatch(session[field]):
            errors.append(f"{field}: 必须是 sprint-N-name")
    if not isinstance(session.get("package"), str) or not session["package"].strip():
        errors.append("package: 必须是非空包名")
    if not isinstance(session.get("outcome"), str) or not session["outcome"].strip():
        errors.append("outcome: 必须是非空任务目标")
    if (
        not isinstance(session.get("acceptance"), list)
        or not session["acceptance"]
        or not all(isinstance(item, str) and item.strip() for item in session.get("acceptance", []))
    ):
        errors.append("acceptance: 必须是非空验收条件数组")
    if session.get("status") not in SESSION_STATUSES:
        errors.append(f"status: 必须是 {sorted(SESSION_STATUSES)}")
    if not errors and session.get("request_digest") != digest(_request(session)):
        errors.append("request_digest: 与依赖请求不一致")
    return errors


def repository_main(root: Path) -> Path:
    outcome = execute(CommandSpec.argv_command(("git", "worktree", "list", "--porcelain"), cwd=root))
    if not outcome.ok:
        raise ValueError(outcome.stderr or "无法读取 Git worktree")
    candidates = [
        Path(line.removeprefix("worktree ")).resolve()
        for line in outcome.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    return next((path for path in candidates if (path / ".git").is_dir()), root.resolve())


def provider_registration(config: dict[str, Any], provider_id: str, capability_id: str) -> dict[str, Any]:
    provider = ((config.get("dependencies") or {}).get("providers") or {}).get(provider_id)
    if not isinstance(provider, dict):
        raise ValueError(f"未登记依赖 Provider: {provider_id}")
    if provider.get("orchestration") != "coordinated":
        raise ValueError(f"Provider {provider_id} 使用异步 Assignment，不允许 coordinated session")
    capabilities = provider.get("capabilities") or {}
    if capability_id not in capabilities:
        raise ValueError(f"Provider {provider_id} 未登记 capability: {capability_id}")
    return provider


def resolve_provider_root(consumer_root: Path, provider: dict[str, Any]) -> Path:
    registered = Path(str(provider["path"]))
    base = repository_main(consumer_root)
    target = registered.resolve() if registered.is_absolute() else (base / registered).resolve()
    if not (target / "config/harness.yml").is_file() or not (target / ".git").exists():
        raise ValueError(f"依赖 Provider 不是有效 Harness Git 工程: {target}")
    return target


def _load_session(consumer_root: Path, session_id: str) -> tuple[StateStore, dict[str, Any]]:
    store = StateStore(consumer_root / ".harness/state/dependency-sessions")
    session = store.read_json(f"{session_id}.json")
    if not isinstance(session, dict) or (errors := validate_session(session)):
        detail = "; ".join(errors) if isinstance(session, dict) else "状态不存在"
        raise ValueError(f"Dependency session 无效: {session_id}: {detail}")
    return store, session


def _bind_provider_plan(session: dict[str, Any]) -> None:
    worktree = Path(session["provider_worktree"]).resolve()
    plan = Path(session["provider_plan"]).resolve()
    if worktree not in plan.parents or not plan.is_file():
        raise ValueError("Provider sprint plan 不在 Provider worktree 内或不存在")
    content = plan.read_text(encoding="utf-8")
    marker = f"dependency_session: {session['session_id']}"
    if marker in content:
        return
    content = content.replace("- **目标**：TODO", f"- **目标**：{session['outcome']}", 1)
    acceptance = "；".join(session["acceptance"])
    anchor = "- **环境就绪**：⬜"
    binding = (
        f"{anchor}\n"
        f"dependency_session: {session['session_id']}\n"
        f"request_digest: {session['request_digest']}\n"
        f"capability: {session['capability_id']}\n"
        f"- **验收条件**：{acceptance}"
    )
    if anchor not in content:
        raise ValueError("Provider sprint plan 缺少标准环境就绪标记")
    plan.write_text(content.replace(anchor, binding, 1), encoding="utf-8")


def _write_provider_session(session: dict[str, Any], bucket: str = "incoming") -> None:
    roots = {Path(session["provider_root"]).resolve()}
    if session.get("provider_worktree"):
        roots.add(Path(session["provider_worktree"]).resolve())
    for root in roots:
        StateStore(root / f".harness/state/dependency-sessions/{bucket}").write_json(
            f"{session['session_id']}.json", session
        )


def start_session(
    consumer_root: Path,
    *,
    session_id: str,
    capability_id: str,
    consumer_sprint: Path,
    consumer_task_id: str,
    provider_project_id: str,
    provider_sprint: str,
) -> dict[str, Any]:
    consumer_root = consumer_root.resolve()
    consumer_sprint = consumer_sprint.resolve()
    if consumer_root not in consumer_sprint.parents or not consumer_sprint.is_file():
        raise ValueError("消费者 Sprint 计划必须位于消费者主工程内")
    config = load_harness_config(force=True, path=consumer_root / "config/harness.yml")
    provider = provider_registration(config, provider_project_id, capability_id)
    provider_root = resolve_provider_root(consumer_root, provider)
    provider_config = load_harness_config(force=True, path=provider_root / "config/harness.yml")
    identity = provider_config.get("project", {})
    if identity.get("id") != provider_project_id or identity.get("type") != "library":
        raise ValueError("Provider project.id/type 与依赖登记不一致")
    content = consumer_sprint.read_text(encoding="utf-8")
    matching = [row for row in table_rows(content) if row.get("id") == consumer_task_id]
    if len(matching) != 1 or (matching[0].get("类型") or matching[0].get("type")) != "dependency-change":
        raise ValueError(f"消费者 Task 不存在于 Sprint: {consumer_task_id}")
    task_row = matching[0]
    outcome = (task_row.get("任务描述") or task_row.get("description") or "").strip()
    acceptance = (task_row.get("验收条件") or task_row.get("acceptance") or "").strip()
    if not outcome or not acceptance:
        raise ValueError("dependency-change Task 必须填写任务描述和验收条件")
    base = {
        "schema_version": 1,
        "session_id": session_id,
        "capability_id": capability_id,
        "consumer_project_id": config["project"]["id"],
        "consumer_sprint": consumer_sprint.stem,
        "consumer_task_id": consumer_task_id,
        "provider_project_id": provider_project_id,
        "provider_sprint": provider_sprint,
        "package": provider["package"],
        "outcome": outcome,
        "acceptance": [acceptance],
    }
    session = {
        **base,
        "request_digest": digest(base),
        "status": "starting",
        "provider_root": str(provider_root),
        "created_at": now(),
    }
    errors = validate_session(session)
    if errors:
        raise ValueError("Dependency session 校验失败:\n- " + "\n- ".join(errors))
    consumer_store = StateStore(consumer_root / ".harness/state/dependency-sessions")
    state_name = f"{session_id}.json"
    existing = consumer_store.read_json(state_name, None)
    if existing is not None:
        if not isinstance(existing, dict) or validate_session(existing):
            raise ValueError(f"Dependency session 已存在但状态无效: {session_id}")
        if existing.get("request_digest") != session["request_digest"]:
            raise ValueError(f"Dependency session ID 已绑定不同请求: {session_id}")
        if existing.get("status") != "failed":
            return existing
        session = {**existing, "status": "starting", "updated_at": now()}
        session.pop("failure", None)
    consumer_store.write_json(state_name, session)
    if session.get("provider_worktree") and session.get("provider_plan"):
        session.update({"status": "provider-planning", "updated_at": now()})
        try:
            _bind_provider_plan(session)
        except (OSError, ValueError) as exc:
            session.update({"status": "failed", "failure": str(exc), "updated_at": now()})
            consumer_store.write_json(state_name, session)
            raise
        consumer_store.write_json(state_name, session)
        _write_provider_session(session)
        return session
    command = (
        "uv",
        "run",
        "--project",
        str(provider_root / ".harness/runtime"),
        "harness",
        "sprint",
        "init",
        provider_sprint,
        "--type",
        "library-sprint",
    )
    outcome = execute(CommandSpec.argv_command(command, cwd=provider_root, timeout_seconds=120))
    if not outcome.ok:
        session.update({"status": "failed", "failure": outcome.stderr or outcome.stdout, "updated_at": now()})
        consumer_store.write_json(state_name, session)
        raise RuntimeError(session["failure"])
    try:
        child = json.loads(outcome.stdout)
    except json.JSONDecodeError as exc:
        session.update({"status": "failed", "failure": "Provider sprint 输出不是 JSON", "updated_at": now()})
        consumer_store.write_json(state_name, session)
        raise ValueError("Provider sprint 输出不是 JSON") from exc
    if not isinstance(child, dict) or not all(isinstance(child.get(field), str) for field in ("worktree", "plan")):
        session.update({"status": "failed", "failure": "Provider sprint 输出缺少 worktree/plan", "updated_at": now()})
        consumer_store.write_json(state_name, session)
        raise ValueError(session["failure"])
    provider_worktree = Path(child["worktree"]).resolve()
    registered_worktrees = (provider_root / str(provider_config["worktree"]["root"])).resolve()
    if registered_worktrees not in provider_worktree.parents:
        session.update({"status": "failed", "failure": "Provider worktree 越出登记目录", "updated_at": now()})
        consumer_store.write_json(state_name, session)
        raise ValueError(session["failure"])
    session.update(
        {
            "status": "provider-planning",
            "provider_worktree": str(provider_worktree),
            "provider_plan": str(Path(child["plan"]).resolve()),
            "updated_at": now(),
        }
    )
    try:
        _bind_provider_plan(session)
    except (OSError, ValueError) as exc:
        session.update({"status": "failed", "failure": str(exc), "updated_at": now()})
        consumer_store.write_json(state_name, session)
        raise
    consumer_store.write_json(state_name, session)
    _write_provider_session(session)
    return session


def record_candidate(consumer_root: Path, session_id: str, artifact: Path, version: str) -> dict[str, Any]:
    store, session = _load_session(consumer_root, session_id)
    if session["status"] not in {"provider-planning", "candidate-built"}:
        raise ValueError(f"当前状态不能登记 candidate: {session['status']}")
    candidate = artifact.resolve()
    provider_worktree = Path(session["provider_worktree"]).resolve()
    if provider_worktree not in candidate.parents or not candidate.is_file():
        raise ValueError("Candidate 必须是 Provider worktree 内的已构建文件")
    if not version.strip():
        raise ValueError("Candidate version 必须是非空字符串")
    package_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    candidate_record: dict[str, Any] = {
        "path": str(candidate),
        "package": session["package"],
        "version": version,
        "sha256": f"sha256:{package_sha}",
    }
    if session["status"] == "candidate-built" and any(
        (session.get("candidate") or {}).get(field) != candidate_record[field]
        for field in ("path", "package", "version", "sha256")
    ):
        raise ValueError("Build Once: 已登记的 candidate 不允许替换")
    package_evidence = StateStore(provider_worktree / ".harness/state/library-packages").read_json(
        f"{session['provider_sprint']}.json"
    )
    if (
        not isinstance(package_evidence, dict)
        or any(package_evidence.get(field) != candidate_record[field] for field in ("package", "version", "sha256"))
        or Path(str(package_evidence.get("artifact") or package_evidence.get("wheel", ""))).resolve() != candidate
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(package_evidence.get("source_commit", "")))
    ):
        raise ValueError("Candidate 与 library-package Build Once 证据不一致")
    candidate_record["source_commit"] = package_evidence["source_commit"]
    if session["status"] == "candidate-built":
        if session.get("candidate") != candidate_record:
            raise ValueError("Build Once: 已登记的 candidate 不允许替换")
        return session
    session.update(
        {
            "status": "candidate-built",
            "candidate": candidate_record,
            "updated_at": now(),
        }
    )
    store.write_json(f"{session_id}.json", session)
    _write_provider_session(session)
    return session


def verify_consumer(consumer_root: Path, session_id: str) -> dict[str, Any]:
    store, session = _load_session(consumer_root, session_id)
    if session.get("status") != "candidate-built":
        raise ValueError("Dependency session 尚未生成 candidate")
    config = load_harness_config(force=True, path=consumer_root / "config/harness.yml")
    provider = provider_registration(config, session["provider_project_id"], session["capability_id"])
    capability = provider["capabilities"][session["capability_id"]]
    evidence = []
    candidate = session["candidate"]
    env = {
        "HARNESS_DEPENDENCY_ARTIFACT": candidate["path"],
        "HARNESS_DEPENDENCY_WHEEL": candidate["path"],
        "HARNESS_DEPENDENCY_PACKAGE": candidate["package"],
        "HARNESS_DEPENDENCY_VERSION": candidate["version"],
        "HARNESS_DEPENDENCY_SHA256": candidate["sha256"],
    }
    for name in capability["consumer_contract_commands"]:
        command = resolve_command(config["commands"][name])
        outcome = execute(CommandSpec.argv_command(command, cwd=consumer_root, env=env, timeout_seconds=600))
        evidence.append(
            {
                "command": name,
                "argv": command,
                "returncode": outcome.returncode,
                "stdout_tail": outcome.stdout[-2000:],
                "stderr_tail": outcome.stderr[-2000:],
            }
        )
        if not outcome.ok:
            session.update({"failure": f"消费者契约失败: {name}", "consumer_evidence": evidence, "updated_at": now()})
            store.write_json(f"{session_id}.json", session)
            raise RuntimeError(session["failure"])
    session.update({"status": "consumer-verified", "consumer_evidence": evidence, "updated_at": now()})
    store.write_json(f"{session_id}.json", session)
    _write_provider_session(session)
    return session


def _session_binding(delivery: dict[str, Any], session: dict[str, Any]) -> bool:
    entries = (delivery.get("satisfies") or {}).get("dependency_sessions") or []
    return any(
        isinstance(item, dict)
        and item.get("session_id") == session["session_id"]
        and item.get("request_digest") == session["request_digest"]
        for item in entries
    )


def _locked_python_package(lock_path: Path, package: str, version: str, package_sha: str) -> bool:
    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    normalized = package.lower().replace("_", "-")
    expected_hash = "sha256:" + package_sha.removeprefix("sha256:")
    for item in document.get("package", []):
        if str(item.get("name", "")).lower().replace("_", "-") != normalized or item.get("version") != version:
            continue
        artifacts = list(item.get("wheels", []))
        if isinstance(item.get("sdist"), dict):
            artifacts.append(item["sdist"])
        return any(artifact.get("hash") == expected_hash for artifact in artifacts)
    return False


def _lock_receipt(stdout: str, lock_path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("consumer_lock_command 必须在最后一行输出 Lock Receipt JSON")
    try:
        receipt = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ValueError("consumer_lock_command 最后一行不是 Lock Receipt JSON") from exc
    if not isinstance(receipt, dict):
        raise ValueError("Lock Receipt 顶层必须是对象")
    actual_lock_sha = "sha256:" + hashlib.sha256(lock_path.read_bytes()).hexdigest()
    expected = {
        "lock": str(lock_path),
        "package": candidate["package"],
        "version": candidate["version"],
        "artifact_sha256": candidate["sha256"],
        "lock_sha256": actual_lock_sha,
    }
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise ValueError("Lock Receipt 未精确绑定 lock/package/version/artifact SHA-256")
    bound = {"schema_version": 1, **expected, "verified_at": now()}
    bound["receipt_digest"] = digest(bound)
    return bound


def complete_session(consumer_root: Path, session_id: str, delivery_path: Path, lock_path: Path) -> dict[str, Any]:
    store, session = _load_session(consumer_root, session_id)
    if session.get("status") != "consumer-verified":
        raise ValueError("Dependency session 尚未通过消费者验证")
    delivery = load_manifest(delivery_path.resolve())
    errors = validate_delivery(delivery)
    provider_worktree = Path(session["provider_worktree"]).resolve()
    errors.extend(validate_delivery_publication(provider_worktree, delivery_path.resolve(), delivery))
    if delivery.get("project_id") != session["provider_project_id"]:
        errors.append("Delivery provider 与 session 不一致")
    if not _session_binding(delivery, session):
        errors.append("Delivery 未绑定当前 dependency session 摘要")
    candidate = session["candidate"]
    if delivery.get("source_commit") != candidate.get("source_commit"):
        errors.append("Delivery source_commit 与 Build Once candidate 不一致")
    artifacts = [item for item in delivery.get("artifacts", []) if item.get("type") == "dependency-package"]
    if not any(
        item.get("package") == candidate["package"]
        and item.get("version") == candidate["version"]
        and str(item.get("sha256", "")).removeprefix("sha256:") == candidate["sha256"].removeprefix("sha256:")
        for item in artifacts
    ):
        errors.append("Delivery dependency-package 与已验证 candidate 不一致")
    verification_path = (
        provider_worktree
        / ".harness/state/delivery-verifications"
        / f"{str(delivery.get('manifest_digest', '')).removeprefix('sha256:')}.json"
    )
    if not verification_path.exists():
        errors.append("Delivery 供应链验证 receipt 不存在")
    else:
        errors.extend(validate_delivery_verification(delivery, load_manifest(verification_path)))
    if errors:
        raise ValueError("Dependency session 完成门禁失败:\n- " + "\n- ".join(errors))
    consumer_config = load_harness_config(force=True, path=consumer_root / "config/harness.yml")
    registration = provider_registration(consumer_config, session["provider_project_id"], session["capability_id"])
    capability = registration["capabilities"][session["capability_id"]]
    lock_command_name = capability.get("consumer_lock_command")
    resolved_lock = lock_path.resolve()
    if consumer_root.resolve() not in resolved_lock.parents or not resolved_lock.is_file():
        raise ValueError("消费者锁文件必须是消费工程内已存在的文件")
    lock_before = hashlib.sha256(resolved_lock.read_bytes()).hexdigest()
    lock_receipt: dict[str, Any] | None = None
    if lock_command_name:
        if diagnostics := command_diagnostics(consumer_config, lock_command_name, consumer_root):
            raise ValueError("消费者锁文件验证命令条件未满足:\n- " + "\n- ".join(diagnostics))
        lock_outcome = execute(
            CommandSpec.argv_command(
                resolve_command(consumer_config["commands"][lock_command_name]),
                cwd=consumer_root,
                env={
                    "HARNESS_DEPENDENCY_ARTIFACT": candidate["path"],
                    "HARNESS_DEPENDENCY_PACKAGE": candidate["package"],
                    "HARNESS_DEPENDENCY_VERSION": candidate["version"],
                    "HARNESS_DEPENDENCY_SHA256": candidate["sha256"],
                    "HARNESS_DEPENDENCY_LOCK": str(resolved_lock),
                },
            )
        )
        if not lock_outcome.ok:
            errors.append(f"消费者锁文件验证命令失败: {lock_command_name}")
        elif hashlib.sha256(resolved_lock.read_bytes()).hexdigest() != lock_before:
            errors.append(f"消费者锁文件验证命令禁止修改锁文件: {lock_command_name}")
        else:
            try:
                lock_receipt = _lock_receipt(lock_outcome.stdout, resolved_lock, candidate)
            except ValueError as exc:
                errors.append(str(exc))
    elif not _locked_python_package(resolved_lock, candidate["package"], candidate["version"], candidate["sha256"]):
        errors.append("消费者锁文件未精确绑定 candidate version 与 SHA-256；非 Python 项目须配置 consumer_lock_command")
    else:
        lock_receipt = _lock_receipt(
            json.dumps(
                {
                    "lock": str(resolved_lock),
                    "package": candidate["package"],
                    "version": candidate["version"],
                    "artifact_sha256": candidate["sha256"],
                    "lock_sha256": "sha256:" + lock_before,
                }
            ),
            resolved_lock,
            candidate,
        )
    if errors:
        raise ValueError("Dependency session 完成门禁失败:\n- " + "\n- ".join(errors))
    receipt_path = StateStore(consumer_root / ".harness/state/dependency-sessions/lock-receipts").write_json(
        f"{session_id}.json", lock_receipt
    )
    session.update(
        {
            "status": "completed",
            "delivery": {
                "path": str(delivery_path.resolve()),
                "delivery_id": delivery["delivery_id"],
                "manifest_digest": delivery["manifest_digest"],
            },
            "lock": str(resolved_lock),
            "lock_receipt": {"path": str(receipt_path), "receipt_digest": lock_receipt["receipt_digest"]},
            "completed_at": now(),
            "updated_at": now(),
        }
    )
    store.write_json(f"{session_id}.json", session)
    _write_provider_session(session, "completed")
    return session


def provider_delivery_guard(provider_root: Path, sprint: str) -> dict[str, Any]:
    plan = provider_root / "docs/exec-plans/active" / f"{sprint}.md"
    if not plan.is_file():
        raise ValueError(f"Library Sprint 计划不存在: {plan}")
    session_id = header_field(plan.read_text(encoding="utf-8"), "dependency_session")
    if not session_id:
        raise ValueError("Library Sprint 缺少 dependency_session")
    session_path = provider_root / ".harness/state/dependency-sessions/incoming" / f"{session_id}.json"
    session = load_manifest(session_path)
    errors = validate_session(session)
    if session.get("status") != "consumer-verified":
        errors.append(f"消费者契约尚未通过: {session.get('status')}")
    if session.get("provider_sprint") != sprint:
        errors.append("dependency session provider_sprint 不匹配")
    config = load_harness_config(force=True, path=provider_root / "config/harness.yml")
    policy = config.get("management", {}) if config["project"]["mode"] == "managed" else config["delivery"]
    directory = policy.get("deliveries_dir") or policy.get("manifests_dir")
    deliveries_root = resolve_project_relative(provider_root, directory, "delivery manifests directory")
    bound: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(deliveries_root.glob("*.json")) if deliveries_root.exists() else []:
        try:
            delivery = load_manifest(path)
        except (OSError, ValueError):
            continue
        if _session_binding(delivery, session):
            bound.append((path, delivery))
    if len(bound) != 1:
        errors.append(f"当前 dependency session 必须唯一绑定一个 Delivery，实际 {len(bound)}")
    if len(bound) == 1:
        delivery_path, delivery = bound[0]
        errors.extend(validate_delivery(delivery))
        errors.extend(validate_delivery_publication(provider_root, delivery_path, delivery))
        if delivery.get("project_id") != session.get("provider_project_id"):
            errors.append("Delivery project_id 与 Provider 不一致")
        candidate = session.get("candidate") or {}
        if delivery.get("source_commit") != candidate.get("source_commit"):
            errors.append("Delivery source_commit 与 Build Once candidate 不一致")
        matching = [item for item in delivery.get("artifacts", []) if item.get("type") == "dependency-package"]
        if not any(
            item.get("package") == candidate.get("package")
            and item.get("version") == candidate.get("version")
            and str(item.get("sha256", "")).removeprefix("sha256:")
            == str(candidate.get("sha256", "")).removeprefix("sha256:")
            for item in matching
        ):
            errors.append("Delivery dependency-package 与 candidate 不一致")
        receipt_path = (
            provider_root
            / ".harness/state/delivery-verifications"
            / f"{str(delivery.get('manifest_digest', '')).removeprefix('sha256:')}.json"
        )
        if not receipt_path.is_file():
            errors.append("Delivery 供应链验证 receipt 不存在")
        else:
            errors.extend(validate_delivery_verification(delivery, load_manifest(receipt_path)))
    if errors:
        raise ValueError("Library Delivery Guard 失败:\n- " + "\n- ".join(errors))
    return {
        "session_id": session_id,
        "delivery": str(bound[0][0]),
        "manifest_digest": bound[0][1]["manifest_digest"],
    }

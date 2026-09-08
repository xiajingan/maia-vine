"""Structured contract and evidence guard for real-environment integration tasks."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.promotion_preflight import validate_promotion_preflight
from mai_harness.runtime.domain.sprint_context import table_rows
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.utils import load_yaml

WRITE_FACETS = frozenset({"data", "schema", "environment"})
WRITE_STRATEGIES = frozenset({"resume", "fix-forward", "restore-checkpoint"})
NONE_VALUES = frozenset({"", "-", "none", "n/a"})
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
IMAGE_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def _row_value(row: dict[str, str], *names: str) -> str:
    return next((str(row.get(name, "")).strip() for name in names if str(row.get(name, "")).strip()), "")


def task_row(sprint_path: Path, task_id: str) -> dict[str, str]:
    rows = table_rows(sprint_path.read_text(encoding="utf-8"))
    return next((row for row in rows if row.get("id") == task_id), {})


def integration_contract(
    sprint_path: Path,
    task_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    row = task_row(sprint_path, task_id)
    raw_facets = _row_value(row, "facets", "任务属性", "能力面")
    facets = [item.strip() for item in raw_facets.replace("，", ",").split(",") if item.strip()]
    if not facets:
        facets = list(task.get("default_facets") or [])
    producer = _row_value(row, "producer", "执行入口") or "none"
    requested_intent = _row_value(row, "write_intent", "写入意图").lower()
    inferred_intent = (
        "controlled-write" if WRITE_FACETS & set(facets) or producer.lower() not in NONE_VALUES else "read-only"
    )
    return {
        "schema_version": 1,
        "environment": "test",
        "facets": facets,
        "write_intent": requested_intent or inferred_intent,
        "producer": producer,
        "checkpoint": _row_value(row, "checkpoint", "检查点") or "none",
        "failure_strategy": _row_value(row, "failure_strategy", "失败策略").lower() or "none",
        "recovery_of": _row_value(row, "recovery_of", "恢复运行") or "none",
        "facets_explicit": bool(raw_facets),
    }


def validate_integration_contract(
    contract: dict[str, Any],
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    require_explicit_facets: bool = False,
) -> list[str]:
    errors: list[str] = []
    facets = contract.get("facets") if isinstance(contract.get("facets"), list) else []
    supported = set(task.get("facets") or [])
    if not facets or not set(facets) <= supported or len(facets) != len(set(facets)):
        errors.append(f"integration facets 必须是无重复的已声明能力面: {sorted(supported)}")
    if require_explicit_facets and contract.get("facets_explicit") is not True:
        errors.append("新 integration 任务必须在 Sprint 行显式声明 facets")
    intent = contract.get("write_intent")
    if intent not in {"read-only", "controlled-write"}:
        errors.append("integration write_intent 只允许 read-only/controlled-write")
    if WRITE_FACETS & set(facets) and intent != "controlled-write":
        errors.append("data/schema/environment facet 必须声明 controlled-write")
    producer = str(contract.get("producer", ""))
    producer_match = re.fullmatch(r"command:([A-Za-z0-9_.-]+)", producer)
    if intent == "controlled-write":
        if not producer_match or not config.get("commands", {}).get(producer_match.group(1)):
            errors.append("写入型 integration producer 必须引用 commands 中已登记的非空 argv：command:<name>")
        if str(contract.get("checkpoint", "")).lower() in NONE_VALUES:
            errors.append("写入型 integration 必须声明 checkpoint")
        if contract.get("failure_strategy") not in WRITE_STRATEGIES:
            errors.append(f"写入型 integration failure_strategy 必须属于 {sorted(WRITE_STRATEGIES)}")
    elif producer.lower() not in NONE_VALUES:
        errors.append("只读 integration 不得声明 producer；任何 producer 都必须使用 controlled-write 合同")
    if "schema" in facets:
        migration = (config.get("integration") or {}).get("migration") or {}
        expected = producer_match.group(1) if producer_match else ""
        if migration.get("mode") != "checkpoint-one-way" or migration.get("command") != expected:
            errors.append("schema facet 必须由 integration.migration.checkpoint-one-way 登记的同一 producer 执行")
    recovery_of = str(contract.get("recovery_of", ""))
    if recovery_of.lower() not in NONE_VALUES and not re.fullmatch(r"[a-f0-9]{32}", recovery_of):
        errors.append("integration recovery_of 必须是上一次失败 attempt 的 32 位 run_id 或 none")
    return errors


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_state_path(root: Path, relative: object) -> Path | None:
    value = Path(str(relative))
    path = (root / value).resolve()
    if value.is_absolute() or ".." in value.parts or not path.is_relative_to(root.resolve()):
        return None
    return path


def build_delivery_identity(root: Path, sprint_id: str) -> tuple[dict[str, Any], list[str]]:
    """Bind build state plus every local artifact or registry image digest."""

    errors: list[str] = []
    build_path = root / ".harness/state" / f"build-image-{sprint_id}.json"
    try:
        build = json.loads(build_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"integration 无法读取 build-image 状态: {build_path}: {exc}"]
    source_commit = str(build.get("source_commit", ""))
    if build.get("success") is not True or not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        errors.append("integration 要求成功且绑定完整 source_commit 的 build-image 状态")
    head = execute(CommandSpec.argv_command(("git", "rev-parse", "HEAD"), cwd=root))
    observed_head = head.stdout.strip() if head.ok else ""
    if observed_head != source_commit:
        errors.append("integration 当前 Git HEAD 必须与 build-image source_commit 完全一致")
    build_tag = str(build.get("tag", ""))
    if build_tag != source_commit:
        errors.append("build-image tag 必须使用完整 source_commit，确保构建与部署身份唯一")
    raw_images = build.get("images")
    images: list[dict[str, str]] = []
    if not isinstance(raw_images, list) or not raw_images:
        errors.append("build-image 状态必须包含至少一个不可变镜像或本地制品")
        raw_images = []
    for index, raw in enumerate(raw_images):
        if not isinstance(raw, dict):
            errors.append(f"build-image images[{index}] 必须是对象")
            continue
        target = str(raw.get("target", ""))
        ref = str(raw.get("ref", ""))
        artifact = str(raw.get("artifact", ""))
        recorded_sha = str(raw.get("artifactSha256", ""))
        digest = str(raw.get("digest", ""))
        if not target or not ref or any(item.get("target") == target for item in images):
            errors.append(f"build-image 镜像 target/ref 缺失或 target 重复: {target or index}")
            continue
        if artifact:
            path = _safe_state_path(root, artifact)
            if path is None or not path.is_file() or not SHA256.fullmatch(recorded_sha):
                errors.append(f"build-image 本地制品路径或摘要无效: {target or index}")
                continue
            actual_sha = _sha256(path)
            if actual_sha != recorded_sha:
                errors.append(f"build-image 本地制品已漂移: {path}")
            images.append(
                {
                    "target": target,
                    "ref": ref,
                    "kind": "artifact",
                    "path": path.relative_to(root.resolve()).as_posix(),
                    "sha256": recorded_sha,
                }
            )
        elif IMAGE_DIGEST.fullmatch(digest):
            images.append({"target": target, "ref": ref, "kind": "registry", "digest": digest})
        else:
            errors.append(f"build-image 镜像缺少可复验 artifact SHA-256 或 registry digest: {target or index}")
    return (
        {
            "build_state": {
                "path": build_path.relative_to(root).as_posix(),
                "sha256": _sha256(build_path),
                "source_commit": source_commit,
                "observed_head": observed_head,
                "tag": build_tag,
            },
            "images": images,
        },
        errors,
    )


def expected_deploy_inputs(build_identity: dict[str, Any]) -> dict[str, Any]:
    """Derive the one deploy input set accepted for a build identity."""

    images = build_identity.get("images") if isinstance(build_identity.get("images"), list) else []
    return {
        "tag": str((build_identity.get("build_state") or {}).get("tag", "")),
        "artifacts": sorted(
            [
                {"name": item["target"], "sha256": f"sha256:{item['sha256']}"}
                for item in images
                if item.get("kind") == "artifact"
            ],
            key=lambda item: item["name"],
        ),
        "registry_digests": sorted([str(item["digest"]) for item in images if item.get("kind") == "registry"]),
    }


def validate_deploy_inputs(
    root: Path,
    sprint_id: str,
    tag: str,
    artifacts: list[dict[str, str]],
    registry_digests: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """Fail before deployment unless CLI inputs exactly match the current build identity."""

    build_identity, errors = build_delivery_identity(root, sprint_id)
    supplied = {
        "tag": tag,
        "artifacts": sorted(
            [{"name": item["name"], "sha256": item["sha256"]} for item in artifacts],
            key=lambda item: item["name"],
        ),
        "registry_digests": sorted(registry_digests),
    }
    if supplied != expected_deploy_inputs(build_identity):
        errors.append("deploy tag、artifact SHA-256 或 registry digest 与当前 build-image 身份不一致")
    return build_identity, errors


def healthy_deploy_states(
    root: Path,
    build_identity: dict[str, Any],
    task_binding: dict[str, Any],
    preflight_receipt: dict[str, Any],
    *,
    not_before: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Return healthy deploy states produced after the current promote-test attempt started."""

    errors: list[str] = []
    records: list[dict[str, str]] = []
    source_commit = str((build_identity.get("build_state") or {}).get("source_commit", ""))
    expected_inputs = expected_deploy_inputs(build_identity)
    for path in sorted((root / ".harness/state").glob("deploy-test-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        tag = str(state.get("tag", ""))
        matching_commit = bool(tag and source_commit.startswith(tag) and re.fullmatch(r"[a-f0-9]{7,40}", tag))
        if (
            matching_commit
            and state.get("success") is True
            and state.get("readiness") == "passed"
            and state.get("smoke") == "passed"
            and state.get("action") == "deploy"
            and str(state.get("ts", "")) >= not_before
            and state.get("task_binding") == task_binding
            and state.get("preflight_receipt") == preflight_receipt
            and state.get("artifacts") == expected_inputs["artifacts"]
            and state.get("registry_digests") == expected_inputs["registry_digests"]
        ):
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256(path),
                    "tag": tag,
                    "node_id": str(state.get("node_id", "")),
                }
            )
    if not records:
        errors.append("promote-test 缺少当前 attempt 内产生、与构建 commit 一致且健康的 Test deploy 状态")
    return records, errors


def _promote_task_ids(sprint_path: Path) -> list[str]:
    return [
        str(row.get("id", ""))
        for row in table_rows(sprint_path.read_text(encoding="utf-8"))
        if (row.get("类型") or row.get("type")) == "promote-test" and row.get("id")
    ]


def _validate_promotion_receipt(
    root: Path,
    sprint_id: str,
    task_id: str,
    build_identity: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    expected_attempt = (root / ".harness/state/tasks" / f"task-{sprint_id}--{task_id}.json").resolve()
    try:
        current_attempt = json.loads(expected_attempt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        current_attempt = {}
    review = current_attempt.get("review") if isinstance(current_attempt, dict) else None
    run_id = str(current_attempt.get("run_id", ""))
    receipt_path = root / ".harness/state/promote-test" / sprint_id / task_id / f"{run_id}.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"integration 无法读取当前 promote-test 回执: {receipt_path}: {exc}"]
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("sprint") != sprint_id
        or receipt.get("task_id") != task_id
        or receipt.get("build_identity") != build_identity
    ):
        errors.append(f"promote-test 回执未绑定当前任务或构建身份: {task_id}")
    attempt = receipt.get("attempt") if isinstance(receipt, dict) else None
    attempt_path = _safe_state_path(root, attempt.get("path", "") if isinstance(attempt, dict) else "")
    if (
        attempt_path != expected_attempt
        or not isinstance(attempt, dict)
        or current_attempt.get("schema_version") != 3
        or current_attempt.get("task_type") != "promote-test"
        or current_attempt.get("task_id") != task_id
        or current_attempt.get("run_id") != attempt.get("run_id")
        or current_attempt.get("attempt") != attempt.get("number")
        or current_attempt.get("ready_at") != attempt.get("ready_at")
        or not isinstance(review, dict)
        or review.get("decision") != "pass"
    ):
        errors.append(f"promote-test 回执未绑定当前已通过的 attempt: {task_id}")
    deploy_records = receipt.get("deploy_states") if isinstance(receipt, dict) else None
    if not isinstance(deploy_records, list) or not deploy_records:
        errors.append(f"promote-test 回执缺少 deploy states: {task_id}")
        deploy_records = []
    task_binding = {
        "sprint": sprint_id,
        "task_id": task_id,
        "attempt_run_id": current_attempt.get("run_id"),
        "attempt": current_attempt.get("attempt"),
    }
    recorded_preflight = receipt.get("preflight_receipt") if isinstance(receipt, dict) else None
    current_preflight, preflight_errors = validate_promotion_preflight(root, "test", task_binding)
    errors.extend(preflight_errors)
    if recorded_preflight != current_preflight:
        errors.append(f"promote-test 回执未绑定当前 attempt 的 Preflight 回执: {task_id}")
    expected_deploy_records, deploy_errors = healthy_deploy_states(
        root,
        build_identity,
        task_binding,
        current_preflight,
        not_before=str(current_attempt.get("ready_at", "")),
    )
    errors.extend(deploy_errors)
    if deploy_records != expected_deploy_records:
        errors.append(f"promote-test 回执未精确绑定当前 attempt 的部署输入与状态: {task_id}")
    source_commit = str((build_identity.get("build_state") or {}).get("source_commit", ""))
    for record in deploy_records:
        path = _safe_state_path(root, record.get("path", "") if isinstance(record, dict) else "")
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = {}
        tag = str(state.get("tag", ""))
        if (
            not isinstance(record, dict)
            or path is None
            or not path.is_file()
            or _sha256(path) != record.get("sha256")
            or state.get("success") is not True
            or state.get("readiness") != "passed"
            or state.get("smoke") != "passed"
            or state.get("action") != "deploy"
            or not re.fullmatch(r"[a-f0-9]{7,40}", tag)
            or not source_commit.startswith(tag)
            or str(state.get("ts", "")) < str((attempt or {}).get("ready_at", ""))
        ):
            errors.append(f"promote-test 回执中的 deploy state 已漂移或不属于当前 attempt: {task_id}")
    binding = {
        "path": receipt_path.relative_to(root).as_posix(),
        "sha256": _sha256(receipt_path) if receipt_path.is_file() else "",
        "task_id": task_id,
        "attempt_run_id": str((attempt or {}).get("run_id", "")),
        "deploy_states": deploy_records,
    }
    return binding, errors


def validate_promote_test_receipt(root: Path, sprint_id: str, task_id: str) -> tuple[dict[str, Any], list[str]]:
    """Validate the current promote-test attempt's run-scoped immutable receipt."""

    build_identity, errors = build_delivery_identity(root, sprint_id)
    binding, receipt_errors = _validate_promotion_receipt(root, sprint_id, task_id, build_identity)
    return binding, [*errors, *receipt_errors]


def delivery_identity(root: Path, sprint_id: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve only task-scoped promote receipts; never infer delivery from global history."""

    build_identity, errors = build_delivery_identity(root, sprint_id)
    sprint_path = next(
        (
            path
            for directory in ("active", "completed")
            if (path := root / "docs/exec-plans" / directory / f"{Path(sprint_id).name}.md").is_file()
        ),
        None,
    )
    if sprint_path is None:
        errors.append(f"integration 无法定位 Sprint 计划: {sprint_id}")
        promote_ids: list[str] = []
    else:
        promote_ids = _promote_task_ids(sprint_path)
        if not promote_ids:
            errors.append("integration 要求 Sprint 中存在 promote-test 任务")
    promotions: list[dict[str, Any]] = []
    for task_id in promote_ids:
        binding, receipt_errors = _validate_promotion_receipt(root, sprint_id, task_id, build_identity)
        promotions.append(binding)
        errors.extend(receipt_errors)
    identity = {
        **build_identity,
        "promote_test_receipts": promotions,
    }
    return identity, errors


def protected_worktree_digest(root: Path, excluded_roots: list[Path]) -> str:
    """Hash project content, index/lstat modes and symlink targets outside this task's outputs."""

    outcome = execute(CommandSpec.argv_command(("git", "ls-files", "-co", "--exclude-standard", "-z"), cwd=root))
    if not outcome.ok:
        return "unversioned"
    index = execute(CommandSpec.argv_command(("git", "ls-files", "-s", "-z"), cwd=root))
    index_modes: dict[str, str] = {}
    if index.ok:
        for entry in (item for item in index.stdout.split("\0") if item):
            metadata, separator, name = entry.partition("\t")
            fields = metadata.split()
            if separator and fields:
                index_modes[name] = fields[0]
    excluded = [path.as_posix().strip("/") for path in excluded_roots]
    digest = hashlib.sha256()
    for raw in sorted(item for item in outcome.stdout.split("\0") if item):
        relative = Path(raw)
        normalized = relative.as_posix()
        if normalized == ".harness" or normalized.startswith(".harness/"):
            continue
        if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in excluded if prefix):
            continue
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            digest.update(normalized.encode())
            digest.update(b"\0")
            digest.update(index_modes.get(normalized, "untracked").encode())
            digest.update(b"\0")
            digest.update(f"{metadata.st_mode:o}".encode())
            digest.update(b"\0")
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(path.readlink().as_posix().encode())
            else:
                digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def validate_integration_receipt(
    root: Path,
    sprint_path: Path,
    task_id: str,
    task: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    contract = integration_contract(sprint_path, task_id, task)
    errors = validate_integration_contract(contract, task, config)
    contract.pop("facets_explicit", None)
    receipt_path = root / "docs/test-reports/integration" / task_id / "receipt.yml"
    try:
        loaded = load_yaml(receipt_path)
    except Exception as exc:  # Receipt is untrusted task output.
        return [f"integration receipt 无法读取: {receipt_path}: {exc}"]
    if not isinstance(loaded, dict):
        return [f"integration receipt 必须是对象: {receipt_path}"]
    if loaded.get("schema_version") != 1 or loaded.get("task_id") != task_id:
        errors.append("integration receipt schema_version/task_id 不匹配")
    if loaded.get("environment") != contract["environment"]:
        errors.append("integration receipt 未绑定合同目标环境")
    if loaded.get("contract") != contract:
        errors.append("integration receipt 未精确绑定当前执行合同")
    identity, identity_errors = delivery_identity(root, sprint_path.stem)
    errors.extend(identity_errors)
    if loaded.get("delivery_identity") != identity:
        errors.append("integration receipt 未精确绑定当前 build/deploy 身份")
    task_attempt_path = root / ".harness/state/tasks" / f"task-{sprint_path.stem}--{task_id}.json"
    try:
        task_attempt = json.loads(task_attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        task_attempt = {}
    run_id = str(task_attempt.get("run_id", ""))
    execution_path = root / ".harness/state/integration-exec" / sprint_path.stem / task_id / f"{run_id}.json"
    try:
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        execution = {}
        errors.append(f"integration producer 执行证据无法读取: {execution_path}: {exc}")
    execution_binding = (
        {
            "path": execution_path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(execution_path.read_bytes()).hexdigest(),
        }
        if execution_path.is_file()
        else {}
    )
    if loaded.get("execution_state") != execution_binding:
        errors.append("integration receipt 未绑定当前 producer 执行证据")
    if (
        not isinstance(execution, dict)
        or execution.get("success") is not True
        or execution.get("status") != "passed"
        or execution.get("contract") != contract
        or execution.get("producer") != contract["producer"]
        or (execution.get("attempt") or {}).get("run_id") != run_id
        or (execution.get("attempt") or {}).get("number") != task_attempt.get("attempt")
        or execution.get("delivery_identity") != identity
        or execution.get("delivery_identity_unchanged") is not True
        or execution.get("protected_worktree_before") != execution.get("protected_worktree_after")
    ):
        errors.append("integration producer 未通过受控执行，或执行期间交付/工作区身份发生变化")
    operations = loaded.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or any(
            not isinstance(item, dict)
            or not item.get("action")
            or item.get("target") != contract["environment"]
            or not item.get("scope")
            or item.get("result") != "passed"
            for item in operations
        )
    ):
        errors.append("integration receipt operations 必须记录通过的 action、target 与 scope")
    expected_action = contract["producer"] if contract["producer"] != "none" else "delivery-identity-preflight"
    if not isinstance(operations, list) or not any(
        isinstance(item, dict) and item.get("action") == expected_action for item in operations
    ):
        errors.append("integration receipt operations 未绑定合同 producer/只读预检入口")
    expected_checkpoint = contract["checkpoint"] if contract["write_intent"] == "controlled-write" else "none"
    if loaded.get("checkpoint") != expected_checkpoint:
        errors.append("integration receipt checkpoint 未绑定当前执行合同")
    failure = loaded.get("failure")
    if (
        not isinstance(failure, dict)
        or type(failure.get("occurred")) is not bool
        or failure.get("strategy") != contract["failure_strategy"]
        or not isinstance(failure.get("details"), str)
        or (failure.get("occurred") is True and not failure.get("details"))
    ):
        errors.append("integration receipt failure 必须记录是否失败、合同策略及失败详情")
    evidence = loaded.get("evidence")
    valid_evidence = isinstance(evidence, list) and bool(evidence)
    phases: set[str] = set()
    output_root = (root / "docs/test-reports/integration" / task_id).resolve()
    evidence_sha256: set[str] = set()
    health_evidence_sha256: set[str] = set()
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            valid_evidence = False
            continue
        relative = Path(str(item.get("path", "")))
        path = (root / relative).resolve()
        phase = str(item.get("phase", ""))
        phases.add(phase)
        evidence_sha256.add(str(item.get("sha256", "")))
        if phase == "health":
            health_evidence_sha256.add(str(item.get("sha256", "")))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(root.resolve())
            or not path.is_relative_to(output_root)
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256")
            or not item.get("summary")
            or item.get("result") != "passed"
        ):
            valid_evidence = False
    if not valid_evidence:
        errors.append("integration receipt evidence 必须绑定工程内存在且摘要匹配的文件")
    required_phases = (
        {"before", "after", "checkpoint", "health"}
        if contract["write_intent"] == "controlled-write"
        else {"verification", "health"}
    )
    if not required_phases <= phases:
        errors.append(f"integration receipt evidence 缺少阶段: {sorted(required_phases - phases)}")
    health = loaded.get("health")
    health_refs = health.get("evidence_sha256") if isinstance(health, dict) else None
    if (
        not isinstance(health, dict)
        or health.get("result") != "passed"
        or not isinstance(health_refs, list)
        or not health_refs
        or not set(health_refs) <= health_evidence_sha256
        or loaded.get("result") != "passed"
    ):
        errors.append("integration receipt 最终 health/result 必须通过并绑定实际 evidence 摘要")
    return errors

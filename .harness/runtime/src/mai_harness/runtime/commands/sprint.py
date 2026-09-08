"""Create and inspect mechanically isolated Sprint workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.integration_contract import (
    integration_contract,
    validate_integration_contract,
)
from mai_harness.runtime.application.requirements import validate_story_confirmations
from mai_harness.runtime.application.sprint_context import (
    legacy_unversioned_contract,
    linked_worktree,
    validate_sprint_context,
)
from mai_harness.runtime.application.task_evidence import (
    current_attempt_state_path,
    load_current_attempt,
    validate_attempt,
)
from mai_harness.runtime.application.worktree_service import create_linked_worktree
from mai_harness.runtime.domain.modes import PROJECT_TYPES
from mai_harness.runtime.domain.sprint_context import (
    SPRINT_ID,
    TASK_ID,
    architecture_domains,
    bootstrap_candidate_digest,
    branch_name,
    build_sprint_outcome_receipt,
    duplicate_sprint_contract_fields,
    source_requirements_digest,
    sprint_header,
    sprint_outcome_path,
    sprint_planning_contract,
    sprint_policy,
    sprint_structure_digest,
    table_rows,
    validate_bootstrap_completion,
    validate_sprint_outcome_evidence,
    validate_sprint_planning_contract,
    validate_task_dependencies,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.technology_config import (
    load_technology_config,
    validate_technology_capabilities,
)
from mai_harness.runtime.infrastructure.utils import load_yaml, write_yaml


def run(root: Path, *argv: str, required: bool = True) -> str:
    outcome = execute(CommandSpec.argv_command(argv, cwd=root))
    if required and not outcome.ok:
        raise RuntimeError(outcome.stderr or outcome.stdout or "命令执行失败")
    return outcome.stdout.strip()


def remote_base(config: dict, policy: dict[str, str]) -> tuple[str, str, str]:
    remote = str((config.get("delivery") or {}).get("remote", "origin"))
    base_key = policy.get("base", "development")
    branch = str(((config.get("delivery") or {}).get("refs") or {}).get(base_key, ""))
    if not remote or not branch:
        raise ValueError(f"无法解析 Sprint 基线: delivery.remote/refs.{base_key}")
    return remote, branch, f"refs/remotes/{remote}/{branch}"


def plan_template(
    sprint_id: str,
    sprint_type: str,
    base_ref: str,
    base_sha: str,
    branch: str,
    requirement_mode: str,
) -> str:
    return (
        f"# {sprint_id}\n\n"
        f"sprint_type: {sprint_type}\n"
        f"base_ref: {base_ref}\n"
        f"base_sha: {base_sha}\n"
        f"branch: {branch}\n\n"
        "planning_contract_version: 3\n"
        f"requirement_mode: {requirement_mode}\n"
        "source_stories: []\n"
        "impact_surfaces: {}\n"
        "delivery_strategy: TODO\n"
        "observable_outcomes: []\n"
        "boss_observation: {entrypoint: TODO, action: TODO}\n"
        "outcome_acceptance: []\n"
        "domain_coverage: {}\n\n"
        "- **目标**：TODO\n"
        "- **状态**：planning\n"
        "- **环境就绪**：⬜\n\n"
        "## 任务\n\n"
        "| ID | 类型 | 来源 | 父任务 | 任务描述 | 依赖 | 产出物 | 验收条件 | 状态 |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )


def _task_row_payload(row: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in row.items() if key not in {"status", "状态"}}


def _task_identity(row: dict[str, str]) -> dict[str, str]:
    return {
        "type": str(row.get("类型") or row.get("type") or ""),
        "origin": str(row.get("来源") or row.get("origin") or ""),
        "parent": str(row.get("父任务") or row.get("parent") or ""),
    }


def init_sprint(root: Path, sprint_id: str, sprint_type: str, *, offline: bool = False) -> dict[str, str]:
    if not SPRINT_ID.fullmatch(sprint_id):
        raise ValueError("Sprint ID 必须使用 sprint-N-name 格式")
    paths = HarnessPaths.detect(project=root)
    rules = load_yaml(paths.rules / "task-rules.yml")
    config = load_harness_config(force=True, path=root / "config/harness.yml")
    technology = load_technology_config(
        path=root / "config/technology.yml",
        defaults_path=paths.framework_config / "technology.defaults.yml",
    )
    mode = config["project"]["mode"]
    project_type = config["project"]["type"]
    allowed_types = set((rules.get("sprint_type_mode_capabilities") or {}).get(mode, []))
    if sprint_type not in allowed_types:
        raise ValueError(f"project.mode={mode} 不允许 Sprint 类型 {sprint_type}")
    allowed_project_types = set((rules.get("sprint_type_project_types") or {}).get(sprint_type, []))
    if project_type not in allowed_project_types:
        raise ValueError(f"Sprint 类型 {sprint_type} 不允许 project.type={project_type}")
    if mode != "control" and (errors := validate_technology_capabilities(technology, config, root)):
        raise ValueError("技术栈能力未就绪:\n- " + "\n- ".join(errors))
    policy = sprint_policy(rules, sprint_type)
    remote, base_branch, base_ref = remote_base(config, policy)
    if not offline:
        run(root, "git", "fetch", "--no-tags", remote, base_branch)
    base_sha = run(root, "git", "rev-parse", f"{base_ref}^{{commit}}")
    branch = branch_name(sprint_id, policy)
    worktree_policy = policy.get("worktree")
    if worktree_policy == "required":
        if linked_worktree(root):
            raise ValueError("必须从主工作区创建新的 Sprint worktree")
        target = root / str(config["worktree"]["root"]) / sprint_id
        create_linked_worktree(root, target, branch, base_sha, sprint_id, config["worktree"])
        project = target
    elif worktree_policy == "forbidden":
        project = root
    else:
        raise ValueError(f"非法 worktree 策略: {worktree_policy}")
    plan = project / "docs/exec-plans/active" / f"{sprint_id}.md"
    requirement_modes = (rules.get("sprint_requirement_modes") or {}).get(sprint_type) or []
    requirement_mode = requirement_modes[0] if len(requirement_modes) == 1 else "TODO"
    try:
        if plan.exists():
            raise ValueError(f"Sprint 计划已存在: {plan}")
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            plan_template(sprint_id, sprint_type, base_ref, base_sha, branch, requirement_mode), encoding="utf-8"
        )
    except Exception:
        if worktree_policy == "required":
            run(root, "git", "worktree", "remove", str(project), required=False)
            run(root, "git", "branch", "-d", branch, required=False)
        raise
    return {
        "sprint_id": sprint_id,
        "sprint_type": sprint_type,
        "worktree": str(project),
        "branch": branch,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "plan": str(plan),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("sprint_id")
    init.add_argument("--type", required=True, dest="sprint_type")
    init.add_argument("--offline", action="store_true")
    for name in ("check", "status", "activate"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
    amend = sub.add_parser("amend")
    amend.add_argument("plan", type=Path)
    amend.add_argument("--reason", required=True)
    migrate_state = sub.add_parser("migrate-state")
    migrate_state.add_argument("plan", type=Path)
    migrate_state.add_argument("--reason", required=True)
    outcome = sub.add_parser("outcome")
    outcome.add_argument("plan", type=Path)
    outcome.add_argument("--task-id", required=True, help="已完成 Review 的 outcome 终态任务 ID")
    outcome.add_argument("--evidence", action="append", required=True, help="工程内实际结果证据文件，可重复")
    bootstrap = sub.add_parser("bootstrap-complete")
    bootstrap.add_argument("--evidence", action="append", required=True, help="已通过的 Sprint outcome receipt，可重复")
    bootstrap_approve = sub.add_parser("bootstrap-approve")
    bootstrap_approve.add_argument("--signoff", required=True, help="Boss 对候选摘要的外部签署文件")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.command == "init":
            payload = init_sprint(root, args.sprint_id, args.sprint_type, offline=args.offline)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"bootstrap-complete", "bootstrap-approve"}:
            config = load_harness_config(force=True, path=root / "config/harness.yml")
            bootstrap_rules = load_yaml(HarnessPaths.detect(project=root).rules / "task-rules.yml")
            architecture = root / "ARCHITECTURE.md"
            domains = architecture_domains(architecture)
            if not domains:
                raise ValueError("ARCHITECTURE.md 必须先声明可解析的领域清单")
            target = root / config["planning"]["bootstrap_completion_receipt"]
            if args.command == "bootstrap-approve":
                candidate = load_yaml(target)
                if not isinstance(candidate, dict) or candidate.get("candidate_sha256") != bootstrap_candidate_digest(
                    candidate
                ):
                    raise ValueError("bootstrap completion 候选不存在或摘要无效；先运行 bootstrap-complete")
                resolved = (root / args.signoff).resolve()
                try:
                    relative = resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError("bootstrap signoff 必须位于工程内") from exc
                if not resolved.is_file():
                    raise ValueError("bootstrap signoff 必须是已存在文件")
                candidate["signoff"] = relative.as_posix()
                planning = dict(config["planning"])
                planning["project_stage"] = "established"
                if completion_errors := validate_bootstrap_completion(
                    root,
                    planning,
                    architecture,
                    candidate,
                    delivery_strategies=bootstrap_rules.get("delivery_strategies"),
                    sprint_delivery_contracts=bootstrap_rules.get("sprint_delivery_contracts"),
                    sprint_requirement_modes=bootstrap_rules.get("sprint_requirement_modes"),
                ):
                    raise ValueError("bootstrap completion 无效:\n- " + "\n- ".join(completion_errors))
                write_yaml(target, candidate)
            else:
                evidence: list[dict[str, str]] = []
                for value in args.evidence:
                    resolved = (root / value).resolve()
                    try:
                        relative = resolved.relative_to(root)
                    except ValueError as exc:
                        raise ValueError(f"bootstrap evidence 必须位于工程内: {value}") from exc
                    if not resolved.is_file() or not relative.name.endswith("-outcome.yml"):
                        raise ValueError(f"bootstrap evidence 必须是 Sprint outcome receipt 文件: {value}")
                    evidence.append(
                        {"path": relative.as_posix(), "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}
                    )
                candidate = {
                    "domains": sorted(domains),
                    "architecture_sha256": hashlib.sha256(architecture.read_bytes()).hexdigest(),
                    "evidence": evidence,
                }
                candidate["candidate_sha256"] = bootstrap_candidate_digest(candidate)
                planning = dict(config["planning"])
                planning["project_stage"] = "established"
                if completion_errors := validate_bootstrap_completion(
                    root,
                    planning,
                    architecture,
                    candidate,
                    require_signoff=False,
                    delivery_strategies=bootstrap_rules.get("delivery_strategies"),
                    sprint_delivery_contracts=bootstrap_rules.get("sprint_delivery_contracts"),
                    sprint_requirement_modes=bootstrap_rules.get("sprint_requirement_modes"),
                ):
                    raise ValueError("bootstrap completion 候选无效:\n- " + "\n- ".join(completion_errors))
                write_yaml(target, candidate)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "receipt": str(target),
                        "next": (
                            "请 Boss 检查候选与 outcome 后签署 candidate_sha256，再运行 bootstrap-approve"
                            if args.command == "bootstrap-complete"
                            else "签署已验证；现在可将 planning.project_stage 改为 established"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        paths = HarnessPaths.detect(project=root)
        plan = args.plan.resolve()
        rules = load_yaml(paths.rules / "task-rules.yml")
        errors = validate_sprint_context(root, plan, rules)
        if errors:
            print(json.dumps({"ok": False, "plan": str(plan), "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        store = StateStore(root / ".harness/state/sprints")
        name = f"{plan.stem}.json"
        current_state = store.read_json(name, None)
        rows = table_rows(plan.read_text(encoding="utf-8"))
        story_confirmations: list[dict[str, str]] | None = None
        task_ids = [row.get("id", "") for row in rows if row.get("id")]
        if (
            len(task_ids) != len(rows)
            or len(task_ids) != len(set(task_ids))
            or any(not TASK_ID.fullmatch(value) for value in task_ids)
        ):
            raise ValueError("Sprint 任务 ID 必须非空、唯一，且仅包含字母、数字、点、下划线或连字符")
        reserved_task_ids = sorted(set(task_ids) & set(rules.get("tasks") or {}))
        if reserved_task_ids:
            raise ValueError(f"Sprint 任务 ID 不得与任务类型同名: {reserved_task_ids}")
        sprint_type = sprint_header(plan)["sprint_type"]
        allowed = set((rules.get("sprint_type_task_capabilities") or {}).get(sprint_type, []))
        invalid_types = [row.get("类型") or row.get("type") for row in rows]
        invalid_types = [value for value in invalid_types if not value or value not in allowed]
        if invalid_types:
            raise ValueError(f"Sprint 包含当前类型不允许的任务: {invalid_types}")
        legacy_only_rows = [
            row
            for row in rows
            if ((rules.get("tasks") or {}).get(row.get("类型") or row.get("type")) or {}).get("lifecycle")
            == "legacy-only"
        ]
        if args.command == "activate" and legacy_only_rows:
            raise ValueError(
                "新 Sprint 不得激活仅供历史计划兼容的任务: "
                f"{sorted(str(row.get('类型') or row.get('type')) for row in legacy_only_rows)}"
            )
        if args.command == "amend" and isinstance(current_state, dict):
            known_task_ids = set(current_state.get("task_ids") or [])
            added_legacy_only = sorted(
                str(row.get("id")) for row in legacy_only_rows if row.get("id") not in known_task_ids
            )
            if added_legacy_only:
                raise ValueError(f"Sprint amend 不得新增仅供历史计划兼容的任务: {added_legacy_only}")
        if args.command in {"check", "activate", "amend", "migrate-state", "outcome"}:
            config = load_harness_config(force=True, path=root / "config/harness.yml")
            project_type = config["project"]["type"]
            project_incompatible = sorted(
                {
                    str(row.get("类型") or row.get("type"))
                    for row in rows
                    if project_type
                    not in set(
                        ((rules.get("tasks") or {}).get(row.get("类型") or row.get("type")) or {}).get(
                            "allowed_project_types"
                        )
                        or PROJECT_TYPES
                    )
                }
            )
            if project_incompatible:
                raise ValueError(f"Sprint 包含 project.type={project_type} 不允许的任务: {project_incompatible}")
            if args.command in {"activate", "amend"}:
                known_task_ids = set(current_state.get("task_ids") or []) if isinstance(current_state, dict) else set()
                integration_rows = [
                    row
                    for row in rows
                    if (row.get("类型") or row.get("type")) == "integration"
                    and (args.command == "activate" or row.get("id") not in known_task_ids)
                ]
                integration_rule = (rules.get("tasks") or {}).get("integration") or {}
                integration_errors = [
                    error
                    for row in integration_rows
                    for error in validate_integration_contract(
                        integration_contract(plan, str(row.get("id")), integration_rule),
                        integration_rule,
                        config,
                        require_explicit_facets=True,
                    )
                ]
                if integration_errors:
                    raise ValueError("Sprint integration 执行合同无效:\n- " + "\n- ".join(integration_errors))
            contract = sprint_planning_contract(plan)
            pending_legacy_migration = (
                args.command == "migrate-state"
                and isinstance(current_state, dict)
                and current_state.get("schema_version") == 1
                and current_state.get("planning_contract_version") in {None, 1}
                and contract.get("planning_contract_version") is None
                and not legacy_unversioned_contract(current_state, contract)
            )
            migrated_legacy_contract = isinstance(current_state, dict) and legacy_unversioned_contract(
                current_state, contract
            )
            if pending_legacy_migration or migrated_legacy_contract:
                if ambiguous_fields := duplicate_sprint_contract_fields(plan):
                    raise ValueError(f"Sprint 合同字段必须且只能声明一次: {ambiguous_fields}")
            else:
                planning_errors = validate_sprint_planning_contract(
                    plan,
                    rows,
                    config["planning"],
                    root / "ARCHITECTURE.md",
                    rules.get("delivery_strategies"),
                    rules.get("sprint_delivery_contracts"),
                    root / "USER_STORIES.md",
                    rules.get("impact_surface_requirements"),
                    rules.get("sprint_requirement_modes"),
                )
                if planning_errors:
                    raise ValueError("Sprint 规划契约无效:\n- " + "\n- ".join(planning_errors))
                if contract.get("planning_contract_version") == 3:
                    dependency_errors = validate_task_dependencies(
                        rows, (rules.get("sprint_type_sequences") or {}).get(sprint_type) or []
                    )
                    if dependency_errors:
                        raise ValueError("Sprint 任务依赖契约无效:\n- " + "\n- ".join(dependency_errors))
                if contract.get("planning_contract_version") == 3 and contract.get("requirement_mode") == "stories":
                    story_confirmations, confirmation_errors = validate_story_confirmations(
                        root, contract.get("source_stories")
                    )
                    if confirmation_errors:
                        raise ValueError("Sprint Story 确认门禁失败:\n- " + "\n- ".join(confirmation_errors))
        digest = sprint_structure_digest(plan)
        if args.command == "migrate-state":
            current = current_state
            if not isinstance(current, dict) or current.get("schema_version") != 1:
                raise ValueError("Sprint 尚未 activate")
            if current.get("structure_sha256") != digest:
                raise ValueError("旧 Sprint 状态迁移前计划必须保持激活时结构；先恢复计划，再迁移身份快照")
            contract = sprint_planning_contract(plan)
            needs_identity_snapshot = not (
                isinstance(current.get("task_identity"), dict) and isinstance(current.get("task_rows"), dict)
            )
            needs_contract_version = "planning_contract_version" not in current and contract.get(
                "planning_contract_version"
            ) in {None, 1}
            needs_legacy_contract = contract.get(
                "planning_contract_version"
            ) is None and not legacy_unversioned_contract(current, contract)
            if not needs_identity_snapshot and not needs_contract_version and not needs_legacy_contract:
                raise ValueError("Sprint 状态已经完成显式迁移，无需迁移")
            migrated: list[str] = []
            if needs_identity_snapshot:
                current["task_identity"] = {row["id"]: _task_identity(row) for row in rows}
                current["task_rows"] = {row["id"]: _task_row_payload(row) for row in rows}
                current.setdefault("retired_task_ids", [])
                migrated.append("task-identity-snapshot")
            if needs_contract_version:
                current["planning_contract_version"] = contract.get("planning_contract_version") or 1
                migrated.append("planning-contract-version")
            if needs_legacy_contract:
                current["legacy_unversioned_contract"] = True
                migrated.append("legacy-unversioned-planning-contract")
            current.setdefault("state_migrations", []).append(
                {
                    "reason": args.reason,
                    "migrated": migrated,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            )
            store.write_json(name, current)
            print(json.dumps({"ok": True, "plan": str(plan), "state": current}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "outcome":
            task_row = next((row for row in rows if row.get("id") == args.task_id), None)
            if task_row is None:
                raise ValueError(f"outcome producer task 未登记: {args.task_id}")
            task_type = task_row.get("类型") or task_row.get("type") or ""
            delivery_contract = rules.get("sprint_delivery_contracts", {}).get(sprint_type) or {}
            terminal_tasks = set(delivery_contract.get("terminal_tasks", []))
            if task_type not in terminal_tasks:
                raise ValueError(f"outcome producer 必须是终态任务 {sorted(terminal_tasks)}，实际 {task_type}")
            if delivery_contract.get("outcome_producer") != "task-review":
                raise ValueError(f"{sprint_type} outcome 必须由 Boss approve 生成")
            task = (rules.get("tasks") or {}).get(task_type, {})
            rules_path = paths.rules / "task-rules.yml"
            if attempt_errors := validate_attempt(root, plan, rules_path, args.task_id, task_type, task):
                raise ValueError("outcome producer Review 未通过:\n- " + "\n- ".join(attempt_errors))
            attempt = load_current_attempt(root, plan, rules_path, args.task_id, task_type, task)
            review_path = root / attempt["review_report"]
            review_relative = review_path.resolve().relative_to(root).as_posix()
            attempt_path = current_attempt_state_path(root, plan, args.task_id)
            producer = {
                "kind": "task-review",
                "task_id": args.task_id,
                "task_type": task_type,
                "review_path": review_relative,
                "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
                "attempt_state_path": attempt_path.resolve().relative_to(root).as_posix(),
                "run_id": attempt["run_id"],
                "review_recorded_at": attempt["review"]["recorded_at"],
            }
            evidence_paths: list[str] = []
            for value in args.evidence:
                candidate = Path(value)
                resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                try:
                    relative = resolved.relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"outcome evidence 必须位于工程内: {value}") from exc
                if not resolved.is_file():
                    raise ValueError(f"outcome evidence 文件不存在: {value}")
                evidence_paths.append(relative.as_posix())
            try:
                review_document = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"outcome producer Review 无法读取: {exc}") from exc
            attestation = review_document.get("outcome") if isinstance(review_document, dict) else None
            receipt, receipt_errors = build_sprint_outcome_receipt(
                root,
                plan,
                evidence_paths,
                producer,
                attestation,
            )
            if receipt_errors:
                raise ValueError("Sprint outcome evidence 无效:\n- " + "\n- ".join(receipt_errors))
            if outcome_errors := validate_sprint_outcome_evidence(
                root,
                plan,
                args.task_id,
                task_type,
                delivery_contract.get("outcome_producer"),
                receipt,
            ):
                raise ValueError("Sprint outcome evidence 无效:\n- " + "\n- ".join(outcome_errors))
            target = sprint_outcome_path(root, plan.stem)
            write_yaml(target, receipt)
            print(json.dumps({"ok": True, "outcome": str(target)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "activate":
            if not task_ids:
                raise ValueError("Sprint activate 前必须至少定义一个任务")
            if store.read_json(name, None) is not None:
                raise ValueError("Sprint 已激活；结构变化必须使用 sprint amend")
            contract = sprint_planning_contract(plan)
            if contract.get("planning_contract_version") != 3:
                raise ValueError("新 Sprint 只允许激活 planning contract v3；v1/v2 仅用于已激活历史 Sprint")
            requirements_sha = (
                source_requirements_digest(root / "USER_STORIES.md", contract.get("source_stories"))
                if contract.get("requirement_mode") == "stories"
                else None
            )
            store.write_json(
                name,
                {
                    "schema_version": 1,
                    "sprint_id": plan.stem,
                    "structure_sha256": digest,
                    "planning_contract_version": 3,
                    "task_ids": task_ids,
                    "task_identity": {row["id"]: _task_identity(row) for row in rows},
                    "task_rows": {row["id"]: _task_row_payload(row) for row in rows},
                    "retired_task_ids": [],
                    "requirements_sha256": requirements_sha,
                    "story_confirmations": story_confirmations or [],
                    "amendments": [],
                    "activated_at": datetime.now(UTC).isoformat(),
                },
            )
        elif args.command == "amend":
            current = store.read_json(name, None)
            if not isinstance(current, dict) or current.get("schema_version") != 1:
                raise ValueError("Sprint 尚未 activate")
            contract = sprint_planning_contract(plan)
            active_contract_version = current.get("planning_contract_version")
            if not legacy_unversioned_contract(current, contract) and (
                contract.get("planning_contract_version") != active_contract_version
            ):
                raise ValueError(
                    "Sprint amend 不得切换 planning contract 版本: "
                    f"active={active_contract_version}, plan={contract.get('planning_contract_version')}"
                )
            known = set(current.get("task_ids", []))
            removed = known - set(task_ids)
            previous_identity = current.get("task_identity")
            previous_rows = current.get("task_rows")
            current_by_id = {str(row.get("id")): row for row in rows}
            if not isinstance(previous_identity, dict) or not isinstance(previous_rows, dict):
                raise ValueError("旧 Sprint 状态缺少任务身份快照；先恢复激活时计划并执行 sprint migrate-state")
            identity_changes = {
                task_id: {"before": previous_identity[task_id], "after": _task_identity(current_by_id[task_id])}
                for task_id in known & set(task_ids)
                if task_id in previous_identity and previous_identity[task_id] != _task_identity(current_by_id[task_id])
            }
            if identity_changes:
                raise ValueError(f"已激活任务的 type/origin/parent 不得变更: {identity_changes}")
            optional_types = {
                task_name
                for stage in (rules.get("sprint_type_sequences") or {}).get(sprint_type, [])
                if isinstance(stage, dict) and stage.get("optional") is True
                for task_name in stage.get("tasks", [])
            }
            for task_id in sorted(removed):
                identity = previous_identity.get(task_id) if isinstance(previous_identity, dict) else None
                if not isinstance(identity, dict) or identity.get("type") not in optional_types:
                    raise ValueError(f"只有尚未执行的可选阶段任务可移除: {task_id}")
                if current_attempt_state_path(root, plan, task_id).exists():
                    raise ValueError(f"已有 attempt 的任务不得移除: {task_id}")
            added = [row for row in rows if row.get("id") not in known]
            retired = set(current.get("retired_task_ids") or [])
            for row in added:
                origin = row.get("来源") or row.get("origin")
                parent = row.get("父任务") or row.get("parent")
                if row.get("id") in retired:
                    raise ValueError(f"已移除任务 ID 不得复用: {row.get('id')}")
                if (
                    origin not in {"scope-split", "remediation"}
                    or not parent
                    or parent == row.get("id")
                    or parent not in known
                ):
                    raise ValueError(f"新增任务 {row.get('id')} 必须声明合法来源和已知父任务")
            amended_requirements_sha = (
                source_requirements_digest(root / "USER_STORIES.md", contract.get("source_stories"))
                if contract.get("requirement_mode") == "stories"
                or (active_contract_version == 2 and sprint_type == "feature-sprint")
                else None
            )
            structure_changed = current.get("structure_sha256") != digest
            requirements_changed = current.get("requirements_sha256") != amended_requirements_sha
            confirmations_changed = (
                story_confirmations is not None and current.get("story_confirmations") != story_confirmations
            )
            if not structure_changed and not requirements_changed and not confirmations_changed:
                raise ValueError("Sprint 结构、需求输入和 Story 确认均未变化，无需 amend")
            current["structure_sha256"] = digest
            current["task_ids"] = task_ids
            current["task_identity"] = {row["id"]: _task_identity(row) for row in rows}
            current["task_rows"] = {row["id"]: _task_row_payload(row) for row in rows}
            current["retired_task_ids"] = sorted(retired | removed)
            current["requirements_sha256"] = amended_requirements_sha
            if story_confirmations is not None:
                current["story_confirmations"] = story_confirmations
            current.setdefault("amendments", []).append(
                {
                    "reason": args.reason,
                    "structure_changed": structure_changed,
                    "requirements_changed": requirements_changed,
                    "confirmations_changed": confirmations_changed,
                    "added": [row.get("id") for row in added],
                    "removed": sorted(removed),
                    "changed": {
                        task_id: {"before": previous_rows[task_id], "after": _task_row_payload(current_by_id[task_id])}
                        for task_id in sorted(known & set(task_ids))
                        if task_id in previous_rows
                        and previous_rows[task_id] != _task_row_payload(current_by_id[task_id])
                    },
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            )
            store.write_json(name, current)
        state = store.read_json(name, None)
        print(json.dumps({"ok": True, "plan": str(plan), "state": state}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Create and inspect mechanically isolated Sprint workspaces."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.sprint_context import linked_worktree, validate_sprint_context
from mai_harness.runtime.application.worktree_service import create_linked_worktree
from mai_harness.runtime.domain.sprint_context import (
    SPRINT_ID,
    TASK_ID,
    branch_name,
    sprint_header,
    sprint_policy,
    sprint_structure_digest,
    table_rows,
)
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.technology_config import (
    load_technology_config,
    validate_technology_capabilities,
)
from mai_harness.runtime.infrastructure.utils import load_yaml


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


def plan_template(sprint_id: str, sprint_type: str, base_ref: str, base_sha: str, branch: str) -> str:
    return (
        f"# {sprint_id}\n\n"
        f"sprint_type: {sprint_type}\n"
        f"base_ref: {base_ref}\n"
        f"base_sha: {base_sha}\n"
        f"branch: {branch}\n\n"
        "- **目标**：TODO\n"
        "- **状态**：planning\n"
        "- **环境就绪**：⬜\n\n"
        "## 任务\n\n"
        "| ID | 类型 | 来源 | 父任务 | 任务描述 | 依赖 | 产出物 | 验收条件 | 状态 |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )


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
    try:
        if plan.exists():
            raise ValueError(f"Sprint 计划已存在: {plan}")
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(plan_template(sprint_id, sprint_type, base_ref, base_sha, branch), encoding="utf-8")
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
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.command == "init":
            payload = init_sprint(root, args.sprint_id, args.sprint_type, offline=args.offline)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        paths = HarnessPaths.detect(project=root)
        plan = args.plan.resolve()
        errors = validate_sprint_context(root, plan, load_yaml(paths.rules / "task-rules.yml"))
        if errors:
            print(json.dumps({"ok": False, "plan": str(plan), "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        store = StateStore(root / ".harness/state/sprints")
        name = f"{plan.stem}.json"
        rows = table_rows(plan.read_text(encoding="utf-8"))
        task_ids = [row.get("id", "") for row in rows if row.get("id")]
        if (
            len(task_ids) != len(rows)
            or len(task_ids) != len(set(task_ids))
            or any(not TASK_ID.fullmatch(value) for value in task_ids)
        ):
            raise ValueError("Sprint 任务 ID 必须非空、唯一，且仅包含字母、数字、点、下划线或连字符")
        sprint_type = sprint_header(plan)["sprint_type"]
        allowed = set(
            (load_yaml(paths.rules / "task-rules.yml").get("sprint_type_task_capabilities") or {}).get(
                sprint_type,
                [],
            )
        )
        invalid_types = [row.get("类型") or row.get("type") for row in rows]
        invalid_types = [value for value in invalid_types if not value or value not in allowed]
        if invalid_types:
            raise ValueError(f"Sprint 包含当前类型不允许的任务: {invalid_types}")
        digest = sprint_structure_digest(plan)
        if args.command == "activate":
            if not task_ids:
                raise ValueError("Sprint activate 前必须至少定义一个任务")
            if store.read_json(name, None) is not None:
                raise ValueError("Sprint 已激活；结构变化必须使用 sprint amend")
            store.write_json(
                name,
                {
                    "schema_version": 1,
                    "sprint_id": plan.stem,
                    "structure_sha256": digest,
                    "task_ids": task_ids,
                    "amendments": [],
                    "activated_at": datetime.now(UTC).isoformat(),
                },
            )
        elif args.command == "amend":
            current = store.read_json(name, None)
            if not isinstance(current, dict) or current.get("schema_version") != 1:
                raise ValueError("Sprint 尚未 activate")
            known = set(current.get("task_ids", []))
            removed = known - set(task_ids)
            if removed:
                raise ValueError(f"已激活任务不得删除: {sorted(removed)}")
            added = [row for row in rows if row.get("id") not in known]
            for row in added:
                origin = row.get("来源") or row.get("origin")
                parent = row.get("父任务") or row.get("parent")
                if origin not in {"scope-split", "remediation"} or not parent or parent not in known:
                    raise ValueError(f"新增任务 {row.get('id')} 必须声明合法来源和已知父任务")
            if current.get("structure_sha256") == digest:
                raise ValueError("Sprint 结构未变化，无需 amend")
            current["structure_sha256"] = digest
            current["task_ids"] = task_ids
            current.setdefault("amendments", []).append(
                {
                    "reason": args.reason,
                    "added": [row.get("id") for row in added],
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

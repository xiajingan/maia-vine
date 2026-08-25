"""Closed public command registry for the installed Harness dispatcher."""

from __future__ import annotations

from dataclasses import dataclass

ALL_MODES = frozenset({"standalone", "managed", "control"})


@dataclass(frozen=True)
class CommandRegistration:
    module: str
    modes: frozenset[str]


def _command(module: str, modes: frozenset[str] = ALL_MODES) -> CommandRegistration:
    return CommandRegistration(module, modes)


# Adding a Python module does not expose it. Every public entry must be reviewed here.
COMMANDS = {
    name.replace("_", "-"): _command(name)
    for name in [
        "action",
        "acceptance_record",
        "autofix",
        "back_merge",
        "branch_env_check",
        "check_contract_strength",
        "check_mock_fixtures",
        "check_prototype_coverage",
        "client_package",
        "code_garden",
        "doc_garden",
        "doc_lint",
        "env_check",
        "framework_drift_check",
        "heartbeat",
        "image_promote",
        "lock",
        "library_package",
        "migrate_deploy_config",
        "migration_check",
        "mode_check",
        "observability_check",
        "pr_adapter",
        "python_architecture_lint",
        "quality_score",
        "run_project_command",
        "secrets_export",
        "secrets_list",
        "secrets_scan",
        "secrets_sync_check",
        "sprint_gate",
        "sprint",
        "task_action",
        "task_context",
        "task_review",
        "test_case_backfill",
        "test_case_validator",
        "ui_audit",
        "ui_tokens_lint",
        "validate_task_rules",
        "verify",
    ]
}
COMMANDS.update(
    {
        "assignment": _command("assignment"),
        "delivery": _command("delivery", frozenset({"standalone", "managed"})),
        "acceptance-record": _command("acceptance_record", frozenset({"standalone", "managed"})),
        "worktree": _command("worktree", frozenset({"standalone", "managed"})),
        "task-rollback": _command("task_rollback", frozenset({"standalone", "managed"})),
        "control": _command("control", frozenset({"control"})),
        "back-merge": _command("back_merge", frozenset({"standalone", "control"})),
        "kubernetes": _command("kubernetes", frozenset({"control"})),
        "test-integration": _command("test_integration", frozenset({"control"})),
        "pipeline": _command("pipeline", frozenset({"standalone"})),
        "deploy": _command("deploy", frozenset({"standalone"})),
        "dependency": _command("dependency", frozenset({"standalone", "managed"})),
        "promote": _command("promote", frozenset({"standalone"})),
        "image-promote": _command("image_promote", frozenset({"standalone"})),
        "release": _command("release", frozenset({"standalone"})),
        "hotfix": _command("hotfix", frozenset({"standalone"})),
        "build-artifact": _command("build_artifact", frozenset({"standalone", "managed"})),
        "build-image": _command("build_image", frozenset({"standalone", "managed"})),
        "promote-prep": _command("promote_prep", frozenset({"standalone"})),
    }
)


def resolve_command(name: str) -> CommandRegistration:
    try:
        return COMMANDS[name]
    except KeyError as exc:
        raise ValueError(f"unknown Harness command: {name}") from exc

"""Stable action IDs decoupling task rules from installed script paths."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter


@dataclass(frozen=True)
class Action:
    command: tuple[str, ...]
    description: str
    phases: frozenset[str]
    modes: frozenset[str] = frozenset({"standalone", "managed", "control"})
    effect: str = "read"
    timeout_seconds: int = 300

    @property
    def parameters(self) -> frozenset[str]:
        return frozenset(field for _, field, _, _ in Formatter().parse(" ".join(self.command)) if field)

    def bind(self, values: dict[str, str] | None = None) -> tuple[str, ...]:
        supplied = values or {}
        missing = self.parameters - supplied.keys()
        if missing:
            raise ValueError(f"Action 缺少参数: {', '.join(sorted(missing))}")
        return tuple(part.format_map(supplied) for part in self.command)


PREFLIGHT = frozenset({"preflight"})
EXECUTE = frozenset({"entry", "execute"})
REVIEW = frozenset({"review", "artifact"})

ACTIONS = {
    "project.verify.preflight": Action(("verify", "preflight"), "项目与依赖预检", PREFLIGHT),
    "project.verify.health": Action(("verify", "health"), "项目健康验证", PREFLIGHT),
    "environment.test.check": Action(("env-check", "check", "test"), "Test 环境检查", PREFLIGHT),
    "environment.prod.check": Action(("env-check", "check", "prod"), "Production 环境检查", PREFLIGHT),
    "environment.test.branch": Action(("branch-env-check", "--env", "test"), "Test 分支身份检查", PREFLIGHT),
    "release.directory.ensure": Action(
        ("action", "release.directory.ensure", "--release", "{sprint}"), "创建当前发布目录", PREFLIGHT, effect="write"
    ),
    "release.notes.exists": Action(
        ("action", "release.notes.exists", "--release", "{sprint}"), "检查当前发布说明", PREFLIGHT
    ),
    "vcs.fetch.main": Action(("action", "vcs.fetch.main"), "获取 main 与 tags", PREFLIGHT, effect="network"),
    "vcs.fetch.release-branches": Action(
        ("action", "vcs.fetch.release-branches"), "获取发布分支", PREFLIGHT, effect="network"
    ),
    "project.quality.score": Action(
        ("quality-score", "--sprint", "{sprint}"), "Sprint 质量评分", EXECUTE, effect="write"
    ),
    "project.acceptance.guard": Action(
        ("acceptance-record", "lint", "{sprint}", "--require-signoff", "--require-approved"),
        "验收记录门禁",
        REVIEW,
        frozenset({"standalone", "managed"}),
    ),
    "control.projects.check": Action(
        ("control", "managed-project-check"), "Managed 注册关系检查", EXECUTE, frozenset({"control"}), "write"
    ),
    "control.assignment.dispatch": Action(
        ("control", "assignment-dispatch", "{manifest}"), "需求派生与文件分发", EXECUTE, frozenset({"control"}), "write"
    ),
    "control.assignment.status": Action(
        ("control", "assignment-status"), "需求纳入结果读取", EXECUTE, frozenset({"control"}), "write"
    ),
    "control.delivery.verify": Action(
        ("control", "delivery-verify", "{manifest}"),
        "校验 Managed 不可变交付",
        EXECUTE,
        frozenset({"control"}),
        "write",
    ),
    "control.release.compose": Action(
        ("control", "release-compose", "{release_id}", "{deliveries}"),
        "组合确定版本系统发布",
        EXECUTE,
        frozenset({"control"}),
        "write",
    ),
    "control.integration.test": Action(
        ("test-integration", "{manifest}"), "执行系统 Test 集成验证", EXECUTE, frozenset({"control"}), "write", 1800
    ),
    "control.test.deploy": Action(
        (
            "kubernetes",
            "{manifest}",
            "--env",
            "test",
            "--execute",
        ),
        "部署 Control Test Release",
        EXECUTE,
        frozenset({"control"}),
        "network",
        1800,
    ),
    "control.integration.finding": Action(
        (
            "control",
            "integration-finding",
            "{manifest}",
            "--summary",
            "{summary}",
            "--project",
            "{project}",
            "--evidence",
            "{evidence}",
        ),
        "固化系统集成问题",
        EXECUTE,
        frozenset({"control"}),
        "write",
    ),
    "control.release.promote": Action(
        (
            "control",
            "release-promote",
            "{manifest}",
        ),
        "提升 Test Verified Release",
        EXECUTE,
        frozenset({"control"}),
        "write",
    ),
    "control.release.rollback": Action(
        (
            "control",
            "release-rollback",
            "{manifest}",
        ),
        "回滚已提升 Release",
        EXECUTE,
        frozenset({"control"}),
        "write",
    ),
}


def resolve_action(action_id: str) -> Action:
    try:
        return ACTIONS[action_id]
    except KeyError as exc:
        raise ValueError(f"未知 Harness action: {action_id}") from exc


def bind_action(action_id: str, values: dict[str, str] | None = None) -> tuple[str, ...]:
    return resolve_action(action_id).bind(values)

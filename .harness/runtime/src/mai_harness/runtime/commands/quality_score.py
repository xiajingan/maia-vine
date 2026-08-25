#!/usr/bin/env python3
"""Collect executable evidence and calculate the Harness 100-point quality score."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.command import harness_command
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.harness_config import (
    load_harness_config,
    resolve_command,
    resolve_command_group,
)
from mai_harness.runtime.infrastructure.technology_config import load_technology_config, unit_command_names
from mai_harness.runtime.infrastructure.utils import load_yaml, try_run

DIMENSION_KEYS = {
    "静态检查": "static",
    "单元测试 + 覆盖率": "unit",
    "集成测试": "integration",
    "服务健康": "health",
    "E2E 测试": "e2e",
    "UI 还原度": "ui_parity",
    "性能基线": "performance",
}


def normalize_sprint_id(value: str) -> str:
    return value if value.startswith("sprint-") else f"sprint-{value}"


def quality_report_basename(value: str) -> str:
    return f"{normalize_sprint_id(value)}-quality"


def normalize_execution(value: Any) -> dict[str, Any]:
    if not value:
        return {"mode": "standard", "env": {}}
    if isinstance(value, str):
        return {"mode": value, "env": {}}
    return {
        "mode": value.get("mode", "standard"),
        "env": value.get("env") if isinstance(value.get("env"), dict) else {},
    }


def load_test_cases(directory: Path) -> list[dict[str, Any]]:
    cases = []
    if not directory.exists():
        return cases
    for file in sorted((*directory.rglob("*.yml"), *directory.rglob("*.yaml"))):
        try:
            case = load_yaml(file)
        except Exception:
            continue
        if not isinstance(case, dict) or not case.get("id") or not case.get("spec"):
            continue
        introduced = case.get("introduced_in") or case.get("sprint") or ""
        modified = case.get("last_modified_in") or introduced
        cases.append(
            {
                **case,
                "introduced_in": introduced,
                "last_modified_in": modified,
                "last_verified_in": case.get("last_verified_in") or modified,
                "sprint": case.get("sprint") or introduced,
                "execution": normalize_execution(case.get("execution")),
                "test_titles": [str(item) for item in case.get("test_titles", [])],
            }
        )
    return cases


def is_current_case(case: dict[str, Any], sprint: str) -> bool:
    current = normalize_sprint_id(sprint)
    return current in [
        normalize_sprint_id(str(case.get(key, "")))
        for key in ("introduced_in", "last_modified_in", "sprint")
        if case.get(key)
    ]


def split_cases_by_runner(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    e2e, standard = [], []
    for case in cases:
        (e2e if re.search(r"(^|/)e2e/|\.e2e\.", str(case.get("spec", "")), re.I) else standard).append(case)
    return e2e, standard


def summarize_playwright_case_run(ok: bool, stdout: str) -> dict[str, str]:
    try:
        report = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {"status": "passed" if ok else "failed", "reason": ""}
    stats = report.get("stats", {})
    annotations = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            annotations.extend(value.get("annotations", []) if isinstance(value.get("annotations"), list) else [])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(report)
    reason = next(
        (
            str(item.get("description", ""))
            for item in annotations
            if item.get("type") == "skip" and item.get("description")
        ),
        "",
    )
    if stats.get("skipped", 0) and not stats.get("expected", 0) and not stats.get("unexpected", 0):
        return {"status": "skipped", "reason": reason}
    return {"status": "passed" if ok and not stats.get("unexpected", 0) else "failed", "reason": ""}


def build_unit_test_env(environment: Mapping[str, str]) -> dict[str, str]:
    excluded = {
        "API_URL",
        "WEB_URL",
        "E2E_MODE",
        "E2E_BASE_URL",
        "E2E_API_URL",
        "E2E_USE_WEBSERVER",
        "E2E_AUTH_MODE",
        "E2E_PASSWORD_LOGIN_USERNAME",
        "E2E_PASSWORD_LOGIN_PASSWORD",
        "TEST_API_BASE_URL",
        "TEST_PUBLIC_BASE_URL",
        "PLAYWRIGHT_BASE_URL",
        "PLAYWRIGHT_API_BASE_URL",
    }
    return {
        key: value
        for key, value in environment.items()
        if key not in excluded and not key.startswith(("TEST_", "QWCHAT_"))
    }


def remote_e2e_env(environment: Mapping[str, str]) -> dict[str, str]:
    output = {
        key: environment[key]
        for key in ("E2E_MODE", "E2E_BASE_URL", "E2E_API_URL", "E2E_AUTH_MODE")
        if environment.get(key)
    }
    if not output.get("E2E_MODE") and environment.get("TEST_API_BASE_URL"):
        output["E2E_MODE"] = "test"
    if not output.get("E2E_BASE_URL") and environment.get("TEST_PUBLIC_BASE_URL"):
        output["E2E_BASE_URL"] = environment["TEST_PUBLIC_BASE_URL"]
    if not output.get("E2E_API_URL") and environment.get("TEST_API_BASE_URL"):
        output["E2E_API_URL"] = environment["TEST_API_BASE_URL"]
    if output.get("E2E_MODE") == "test":
        output["E2E_USE_WEBSERVER"] = "0"
    output["PLAYWRIGHT_REUSE_EXISTING_SERVER"] = "true"
    return output


def build_playwright_command(
    specs: list[str] | None = None, grep: str = "", environment: Mapping[str, str] | None = None, reporter: str = ""
) -> str:
    env = remote_e2e_env({**os.environ, **(environment or {})})
    prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    command = f"{prefix} pnpm test:e2e" + (" " + " ".join(shlex.quote(item) for item in (specs or [])) if specs else "")
    if grep:
        command += f" --grep {shlex.quote(grep)}"
    if reporter:
        command += f" --reporter={reporter}"
    return command.strip()


@dataclass
class Score:
    sprint: str
    level: str
    threshold: int
    weights: dict[str, int]
    details: list[tuple[str, int, int]] = field(default_factory=list)
    hard_failures: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    def add(self, label: str, value: int, section: str = "") -> None:
        self.details.append((label, value, self.weights[label]))
        self.sections.append(section)

    @property
    def total(self) -> int:
        return sum(value for _, value, _ in self.details)

    @property
    def passed(self) -> bool:
        return self.total >= self.threshold and not self.hard_failures


def coverage_percent(directory: Path) -> float:
    generic = directory / "harness-coverage.json"
    if generic.exists():
        try:
            return float(json.loads(generic.read_text())["percent"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return 0.0
    summary = directory / "coverage-summary.json"
    if summary.exists():
        try:
            return float(json.loads(summary.read_text())["total"]["lines"]["pct"])
        except (KeyError, ValueError, json.JSONDecodeError):
            pass
    python_coverage = directory / "coverage.json"
    if python_coverage.exists():
        try:
            return float(json.loads(python_coverage.read_text())["totals"]["percent_covered"])
        except (KeyError, ValueError, json.JSONDecodeError):
            pass
    return 0.0


def calculate(sprint: str, level: str, threshold: int, root: Path, coverage_dir: Path) -> Score:
    harness = load_harness_config()
    paths = HarnessPaths.detect(project=root)
    dimensions = harness["quality"]["dimensions"]
    weights = {value["label"]: value["weight"] for value in dimensions.values()}
    score = Score(sprint, level, threshold, weights)
    commands = harness["commands"]
    static_commands = resolve_command_group(harness, "static", root, require_conditions=True)
    static_results = [try_run(command, cwd=root) for command in static_commands]
    static_ok = bool(static_results) and all(result.ok for result in static_results)
    static = weights["静态检查"] if static_ok else 0
    score.add("静态检查", static, f"- Commands: {len(static_commands)}\n- Passed: {static_ok}")
    technology = load_technology_config(
        path=root / "config/technology.yml",
        defaults_path=paths.framework_config / "technology.defaults.yml",
    )
    unit_names = unit_command_names(technology, harness["project"]["type"])
    unit_commands = [resolve_command(commands.get(name, [])) for name in unit_names]
    unit_ok = bool(unit_commands) and all(
        command and try_run(command, cwd=root, env=build_unit_test_env(os.environ)).ok for command in unit_commands
    )
    coverage = coverage_percent(coverage_dir)
    unit_max = weights["单元测试 + 覆盖率"]
    unit = (
        unit_max
        if unit_ok and coverage >= 80
        else round(unit_max * 0.75)
        if unit_ok and coverage >= 60
        else round(unit_max * 0.5)
        if unit_ok
        else 0
    )
    score.add(
        "单元测试 + 覆盖率",
        unit,
        f"- Commands: {', '.join(unit_names)}\n- 测试通过: {unit_ok}\n- 覆盖率: {coverage:.1f}%",
    )
    integration_files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and re.search(r"(?:integration|\.int\.)[^/]*\.(?:test|spec)\.", path.name)
        and not {"node_modules", ".git", "coverage"}.intersection(path.parts)
    ]
    integration_command = resolve_command(commands.get("integration", []))
    integration_ok = bool(integration_command and try_run(integration_command, cwd=root).ok)
    integration = (
        weights["集成测试"]
        if integration_ok
        else round(weights["集成测试"] * 0.5)
        if level == "L1" and not integration_files and not integration_command
        else 0
    )
    if level != "L1" and not integration_files and not integration_command:
        score.hard_failures.append(f"{level} 要求集成测试，但未找到入口")
    score.add("集成测试", integration, f"- 测试文件: {len(integration_files)}\n- 通过: {integration_ok}")
    health = try_run(
        [
            *harness_command("verify", "health"),
            "--no-start",
            "--report-dir",
            str(root / ".harness/verify-reports"),
        ],
        cwd=root,
    )
    health_score = weights["服务健康"] if health and health.ok else 0
    if not health_score:
        score.hard_failures.append("服务健康检查未通过")
    score.add("服务健康", health_score, f"- 检查结果: {bool(health and health.ok)}")
    cases = load_test_cases(paths.test_cases)
    current = [case for case in cases if is_current_case(case, sprint)]
    e2e_cases, _ = split_cases_by_runner(current)
    fallback = sorted(paths.e2e.rglob("*.spec.*")) if paths.e2e.exists() else []
    configured_e2e = resolve_command(commands.get("e2e", []))
    if harness.get("gates", {}).get("require_e2e") is False:
        e2e_ok = True
    elif configured_e2e:
        e2e_ok = try_run(configured_e2e, cwd=root).ok
    elif e2e_cases:
        e2e_ok = True
        for case in e2e_cases:
            titles = case.get("test_titles") or ([case.get("title")] if case.get("title") else [case["id"]])
            grep = "|".join(re.escape(str(item)) for item in titles)
            run = try_run(
                build_playwright_command([case["spec"]], grep, case["execution"].get("env", {}), "json"), cwd=root
            )
            result = summarize_playwright_case_run(run.ok, run.stdout)
            e2e_ok = e2e_ok and result["status"] == "passed"
    elif fallback:
        e2e_ok = try_run(build_playwright_command(), cwd=root).ok
    else:
        e2e_ok = False
    score.add("E2E 测试", weights["E2E 测试"] if e2e_ok else 0, f"- 当前用例: {len(current)}\n- 结果: {e2e_ok}")
    ui_applicable = harness["project"]["type"] in dimensions["ui_parity"].get("applies_to", [])
    ui_commands = [
        harness_command("check-prototype-coverage", "--sprint", sprint),
        harness_command("check-contract-strength", "--sprint", sprint),
        [
            *harness_command("ui-audit"),
            "--sprint",
            sprint,
            "--report-path",
            str(coverage_dir / "ui-audit.json"),
            "--screenshot-dir",
            str(coverage_dir / "ui-audit"),
        ],
    ]
    ui_gate_results = [try_run(command, cwd=root).ok for command in ui_commands] if ui_applicable else [True] * 3
    audit_path = coverage_dir / "ui-audit.json"
    ui_ok = not ui_applicable
    if ui_applicable and audit_path.exists():
        try:
            audit = json.loads(audit_path.read_text())
            ui_ok = (
                all(ui_gate_results)
                and audit.get("passed") is True
                and (audit.get("required") is False or bool(audit.get("pages")))
            )
        except json.JSONDecodeError:
            pass
    score.add(
        "UI 还原度",
        weights["UI 还原度"] if ui_ok else 0,
        f"- 原型覆盖: {ui_gate_results[0]}\n- 契约强度: {ui_gate_results[1]}\n- prototype-parity: {ui_ok}",
    )
    performance = (coverage_dir / "lighthouse.json").exists() or (coverage_dir / "k6-results.json").exists()
    perf_max = weights["性能基线"]
    perf_score = perf_max if performance else 0 if level == "L3" else round(perf_max * 0.5)
    if level == "L3" and not performance:
        score.hard_failures.append("L3 要求性能基线")
    score.add("性能基线", perf_score, f"- 性能产物: {performance}")
    return score


def write_report(score: Score, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    base = quality_report_basename(score.sprint)
    markdown = directory / f"{base}.md"
    summary = directory / f"{base}.json"
    rows = "\n".join(f"| {label} | {value} | {maximum} |" for label, value, maximum in score.details)
    markdown.write_text(
        "\n".join(
            [
                f"# Sprint {score.sprint} 质量评分报告",
                "",
                f"- **层级**: {score.level}（CICD.md）",
                f"- **生成时间**: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                f"- **阈值**: {score.threshold} 分",
                f"- **结果**: {'✅ 达标' if score.passed else '❌ 不达标'} ({score.total}/100)",
                f"- **硬门禁失败**: {'；'.join(score.hard_failures) if score.hard_failures else '无'}",
                "",
                "## 评分明细",
                "",
                "| 步骤 | 得分 | 满分 |",
                "| --- | ---: | ---: |",
                rows,
                "",
                "## 详细报告",
                "",
                *score.sections,
                "",
                f"**结果: {'✅ 达标' if score.passed else '❌ 不达标'}**",
                "",
            ]
        ),
        encoding="utf-8",
    )
    data = {
        "sprint": score.sprint,
        "level": score.level,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "threshold": score.threshold,
        "total": score.total,
        "max": 100,
        "passed": score.passed,
        "hard_failures": score.hard_failures,
        "details": [
            {"label": label, "score": value, "max": maximum, "weight": f"{maximum}%"}
            for label, value, maximum in score.details
        ],
    }
    summary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--sprint")
    identity.add_argument("--release")
    parser.add_argument("--level", choices=("L1", "L2", "L3"), default="L1")
    parser.add_argument("--report-dir", type=Path, default=Path("docs/test-reports"))
    parser.add_argument("--threshold", type=int)
    parser.add_argument("--coverage-dir", type=Path, default=Path("coverage"))
    args = parser.parse_args()
    config = load_harness_config()
    sprint = args.sprint or f"release-{args.release}"
    threshold = (
        args.threshold if args.threshold is not None else int(config.get("gates", {}).get("quality_threshold", 95))
    )
    try:
        score = calculate(sprint, args.level, threshold, Path.cwd(), args.coverage_dir)
    except ValueError as exc:
        dimensions = config["quality"]["dimensions"]
        weights = {value["label"]: value["weight"] for value in dimensions.values()}
        score = Score(sprint, args.level, threshold, weights, hard_failures=[str(exc)])
        for label in weights:
            score.add(label, 0, f"- Hard failure: {exc}" if label == "静态检查" else "- 未执行")
    report, _ = write_report(score, args.report_dir)
    print(f"总分: {score.total}/100 — {'达标' if score.passed else '不达标'}；报告: {report}")
    return 0 if score.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

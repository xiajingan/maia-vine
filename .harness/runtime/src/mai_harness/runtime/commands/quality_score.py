#!/usr/bin/env python3
"""Collect executable evidence and calculate the Harness 100-point quality score."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.application.performance_evidence import collect_backend_performance
from mai_harness.runtime.infrastructure.core.command import harness_command
from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.process import ManagedProcess
from mai_harness.runtime.infrastructure.harness_config import (
    load_harness_config,
    resolve_command,
    resolve_command_group,
)
from mai_harness.runtime.infrastructure.technology_config import load_technology_config, unit_command_names
from mai_harness.runtime.infrastructure.utils import CommandResult, load_yaml, try_run

DIMENSION_KEYS = {
    "静态检查": "static",
    "单元测试 + 覆盖率": "unit",
    "集成测试": "integration",
    "服务健康": "health",
    "E2E 测试": "e2e",
    "UI 还原度": "ui_parity",
    "性能基线": "performance",
}
RUNTIME_POLL_SECONDS = 0.1
HANDOFF_MODE = 0o600
RESERVED_HANDOFF_PREFIX = "HARNESS_QUALITY_"


class QualityRuntimeSignal(RuntimeError):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"质量 runtime 收到信号 {signum}")

    @property
    def exit_code(self) -> int:
        return 128 + self.signum


def _process_group_alive(process: ManagedProcess) -> bool:
    process.process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _endpoint_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _terminate_process_group(process: ManagedProcess, timeout: float) -> bool:
    if not _process_group_alive(process):
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while _process_group_alive(process) and time.monotonic() < deadline:
        time.sleep(RUNTIME_POLL_SECONDS)
    if _process_group_alive(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        kill_deadline = time.monotonic() + timeout
        while _process_group_alive(process) and time.monotonic() < kill_deadline:
            time.sleep(RUNTIME_POLL_SECONDS)
    if _process_group_alive(process):
        return False
    process.process.wait()
    return True


def run_runtime_command(
    command: list[str] | str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> CommandResult:
    argv = ["/bin/sh", "-c", command] if isinstance(command, str) else command
    process = ManagedProcess.start(argv, cwd=cwd, env=environment)
    try:
        deadline = time.monotonic() + timeout
        while process.running() and time.monotonic() < deadline:
            time.sleep(RUNTIME_POLL_SECONDS)
        if process.running():
            terminated = _terminate_process_group(process, timeout)
            message = "quality runtime consumer timeout" + ("; process group residual" if not terminated else "")
            return CommandResult(False, "", message, 124)
    except BaseException:
        _terminate_process_group(process, timeout)
        raise
    returncode = process.process.wait()
    return CommandResult(returncode == 0, "", "", returncode)


@dataclass
class ManagedQualityRuntime:
    root: Path
    harness: dict[str, Any]
    process: ManagedProcess
    run_id: str
    temp_directory: Path
    handoff_path: Path
    base_environment: dict[str, str]
    started_ns: int
    environment: dict[str, str] = field(default_factory=dict)
    finalized: bool = False
    force_kill: bool = False

    @classmethod
    def start(cls, root: Path, harness: dict[str, Any]) -> ManagedQualityRuntime:
        runtime = harness["quality"]["runtime"]
        command = resolve_command(harness["commands"].get(runtime["command"], []))
        if not command:
            raise ValueError("quality.runtime.command 未解析为非空命令")
        temp_directory = Path(tempfile.mkdtemp(prefix="harness-quality-"))
        temp_directory.chmod(0o700)
        handoff_path = temp_directory / "environment.json"
        run_id = uuid.uuid4().hex
        environment = {
            **os.environ,
            "HARNESS_QUALITY_RUN_ID": run_id,
            "HARNESS_QUALITY_ENV_PATH": str(handoff_path),
        }
        started_ns = time.time_ns()
        try:
            process = ManagedProcess.start(command, cwd=root, env=environment)
        except QualityRuntimeSignal:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise
        except BaseException as exc:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise ValueError("质量 runtime 启动失败") from exc
        return cls(root, harness, process, run_id, temp_directory, handoff_path, environment, started_ns)

    def _load_handoff(self) -> dict[str, str]:
        runtime = self.harness["quality"]["runtime"]
        if not runtime["environment_handoff"]:
            return {}
        try:
            metadata = self.handoff_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError("质量 runtime 环境 handoff 缺失") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("质量 runtime 环境 handoff 必须是普通文件")
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != HANDOFF_MODE:
            raise ValueError("质量 runtime 环境 handoff 所有者或权限不安全")
        if metadata.st_mtime_ns < self.started_ns:
            raise ValueError("质量 runtime 环境 handoff 已过期")
        try:
            document = json.loads(self.handoff_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("质量 runtime 环境 handoff 不是合法 JSON") from exc
        if not isinstance(document, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) for key, value in document.items()
        ):
            raise ValueError("质量 runtime 环境 handoff 必须是 string map")
        if any(key.startswith(RESERVED_HANDOFF_PREFIX) for key in document):
            raise ValueError("质量 runtime 环境 handoff 禁止覆盖保留变量")
        return document

    def wait_ready(self) -> dict[str, str]:
        runtime = self.harness["quality"]["runtime"]
        deadline = time.monotonic() + float(runtime["startup_timeout_seconds"])
        verification = self.harness["verification"]
        last_handoff_error: ValueError | None = None
        while time.monotonic() < deadline:
            if not self.process.running():
                raise ValueError(f"质量 runtime 提前退出: {self.process.process.returncode}")
            try:
                handoff = self._load_handoff()
                last_handoff_error = None
            except ValueError as exc:
                last_handoff_error = exc
                time.sleep(RUNTIME_POLL_SECONDS)
                continue
            merged = {**self.base_environment, **handoff}
            api_url = handoff.get("API_URL", str(verification.get("api_url", ""))).rstrip("/")
            web_url = handoff.get("WEB_URL", str(verification.get("web_url", ""))).rstrip("/")
            endpoints = []
            if api_url:
                endpoints.extend(
                    [
                        api_url + str(verification.get("health_endpoint", "/health")),
                        api_url + str(verification.get("ready_endpoint", "/ready")),
                    ]
                )
            if web_url:
                endpoints.append(web_url)
            if all(_endpoint_ok(endpoint) for endpoint in endpoints):
                self.environment = merged
                return merged
            time.sleep(RUNTIME_POLL_SECONDS)
        if last_handoff_error:
            raise last_handoff_error
        raise ValueError("质量 runtime readiness 超时")

    def finalize(self) -> list[str]:
        if self.finalized:
            return []
        self.finalized = True
        runtime = self.harness["quality"]["runtime"]
        timeout = float(runtime["shutdown_timeout_seconds"])
        failures: list[str] = []
        if _process_group_alive(self.process):
            try:
                os.killpg(self.process.pid, signal.SIGKILL if self.force_kill else signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout
            while _process_group_alive(self.process) and time.monotonic() < deadline:
                time.sleep(RUNTIME_POLL_SECONDS)
            if _process_group_alive(self.process):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                kill_deadline = time.monotonic() + timeout
                while _process_group_alive(self.process) and time.monotonic() < kill_deadline:
                    time.sleep(RUNTIME_POLL_SECONDS)
        if _process_group_alive(self.process):
            failures.append("质量 runtime 进程组无法在清理时限内回收")
        else:
            self.process.process.wait()
        cleanup_name = runtime.get("cleanup_command", "")
        if cleanup_name:
            cleanup = resolve_command(self.harness["commands"].get(cleanup_name, []))
            result = try_run(cleanup, cwd=self.root, env=self.base_environment, timeout=timeout)
            if not result.ok:
                failures.append(f"质量 runtime cleanup command 失败 ({result.returncode})")
        try:
            self.handoff_path.unlink(missing_ok=True)
            shutil.rmtree(self.temp_directory)
        except OSError:
            failures.append("质量 runtime 私有 handoff/temp 清理失败")
        if self.handoff_path.exists() or self.temp_directory.exists():
            failures.append("质量 runtime 私有 handoff/temp 仍有残留")
        return failures


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
    evidence: dict[str, Any] = field(default_factory=dict)

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


def dimension_applies(dimensions: dict[str, Any], key: str, project_type: str) -> bool:
    applies_to = dimensions[key].get("applies_to")
    return applies_to is None or project_type in applies_to


def is_integration_test_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return bool(
        re.search(r"(^|/)(?:integration|int)(?:/|[._-])", relative, re.I)
        or re.search(r"(?:\.integration|\.int)\.(?:test|spec)\.", path.name, re.I)
    )


def legacy_frontend_performance_artifact(project_type: str, coverage_dir: Path) -> bool:
    return project_type in {"frontend", "fullstack"} and (
        (coverage_dir / "lighthouse.json").is_file() or (coverage_dir / "k6-results.json").is_file()
    )


def runtime_dimensions_apply(harness: dict[str, Any]) -> bool:
    dimensions = harness["quality"]["dimensions"]
    project_type = harness["project"]["type"]
    return any(
        dimension_applies(dimensions, key, project_type) for key in ("health", "e2e", "ui_parity", "performance")
    )


def _artifact_mime(path: Path) -> str:
    prefix = path.read_bytes()[:8]
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if path.suffix == ".json":
        return "application/json"
    return "application/octet-stream"


def load_action_evidence(root: Path, artifact_path: str, expected_run_id: str) -> dict[str, Any]:
    root = root.resolve()
    manifest = (root / artifact_path).resolve()
    if root not in manifest.parents or not manifest.is_file() or (root / artifact_path).is_symlink():
        raise ValueError("quality action evidence manifest 路径无效")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("quality action evidence manifest 不是合法 JSON") from exc
    live_cases = document.get("liveCases") if isinstance(document, dict) else None
    artifacts = document.get("artifacts") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("runId") != expected_run_id
        or document.get("passed") is not True
        or not isinstance(live_cases, list)
        or not live_cases
        or any(not isinstance(item, dict) or item.get("status") != "pass" for item in live_cases)
        or not isinstance(artifacts, list)
        or not artifacts
    ):
        raise ValueError("quality action evidence manifest 结果或 run ID 无效")
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("quality action evidence artifact 格式无效")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or relative in seen:
            raise ValueError("quality action evidence artifact 路径缺失或重复")
        seen.add(relative)
        raw_path = Path(relative)
        target = (root / raw_path).resolve()
        if raw_path.is_absolute() or ".." in raw_path.parts or root not in target.parents:
            raise ValueError("quality action evidence artifact 路径越界")
        source = root / raw_path
        if source.is_symlink() or not source.is_file():
            raise ValueError("quality action evidence artifact 不是工程内普通文件")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if item.get("sha256") != digest or item.get("mimeType") != _artifact_mime(source):
            raise ValueError("quality action evidence artifact digest 或 MIME 不匹配")
    return document


def collect_action_evidence(
    score: Score,
    root: Path,
    harness: dict[str, Any],
    run_consumer: Callable[[list[str] | str], CommandResult],
    environment: Mapping[str, str] | None,
) -> None:
    config = harness["quality"].get("action_evidence", {})
    command_name = config.get("command", "")
    if not command_name:
        return
    command = resolve_command(harness["commands"].get(command_name, []))
    result = run_consumer(command)
    try:
        if not result.ok:
            raise ValueError("quality action evidence producer 失败")
        run_id = (environment or {}).get("HARNESS_QUALITY_RUN_ID", "")
        if not run_id:
            raise ValueError("quality action evidence 缺少 quality run ID")
        score.evidence["action_evidence"] = load_action_evidence(root, config["artifact"], run_id)
    except ValueError as exc:
        score.hard_failures.append(str(exc))


def calculate_runtime_dimensions(
    score: Score,
    sprint: str,
    level: str,
    root: Path,
    coverage_dir: Path,
    harness: dict[str, Any],
    paths: HarnessPaths,
    *,
    environment: Mapping[str, str] | None = None,
    managed_runtime: bool = False,
) -> None:
    dimensions = harness["quality"]["dimensions"]
    project_type = harness["project"]["type"]
    commands = harness["commands"]
    weights = score.weights
    consumer_timeout = float(harness["quality"]["runtime"]["startup_timeout_seconds"])

    def run_consumer(command: list[str] | str) -> CommandResult:
        if managed_runtime:
            return run_runtime_command(
                command,
                cwd=root,
                environment=environment or {},
                timeout=consumer_timeout,
            )
        return try_run(command, cwd=root, env=environment)

    health_applicable = dimension_applies(dimensions, "health", project_type)
    health_command = [
        *harness_command("verify", "health"),
        *(["--no-start"] if managed_runtime else []),
        "--report-dir",
        str(root / ".harness/verify-reports"),
    ]
    health = run_consumer(health_command) if health_applicable else None
    health_score = weights["服务健康"] if not health_applicable or (health and health.ok) else 0
    if health_applicable and not health_score:
        score.hard_failures.append("服务健康检查未通过")
    score.add(
        "服务健康",
        health_score,
        f"- 适用: {health_applicable}\n- 检查结果: {bool(health and health.ok)}",
    )
    cases = load_test_cases(paths.test_cases)
    current = [case for case in cases if is_current_case(case, sprint)]
    e2e_cases, _ = split_cases_by_runner(current)
    fallback = sorted(paths.e2e.rglob("*.spec.*")) if paths.e2e.exists() else []
    configured_e2e = resolve_command(commands.get("e2e", []))
    e2e_applicable = dimension_applies(dimensions, "e2e", project_type)
    if not e2e_applicable or harness.get("gates", {}).get("require_e2e") is False:
        e2e_ok = True
    elif configured_e2e:
        e2e_ok = run_consumer(configured_e2e).ok
    elif e2e_cases:
        e2e_ok = True
        for case in e2e_cases:
            titles = case.get("test_titles") or ([case.get("title")] if case.get("title") else [case["id"]])
            grep = "|".join(re.escape(str(item)) for item in titles)
            run = run_consumer(build_playwright_command([case["spec"]], grep, case["execution"].get("env", {}), "json"))
            e2e_ok = e2e_ok and summarize_playwright_case_run(run.ok, run.stdout)["status"] == "passed"
    elif fallback:
        e2e_ok = run_consumer(build_playwright_command()).ok
    else:
        e2e_ok = False
    score.add(
        "E2E 测试",
        weights["E2E 测试"] if e2e_ok else 0,
        f"- 适用: {e2e_applicable}\n- 当前用例: {len(current)}\n- 结果: {e2e_ok}",
    )
    ui_applicable = dimension_applies(dimensions, "ui_parity", project_type)
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
    ui_gate_results = [run_consumer(command).ok for command in ui_commands] if ui_applicable else [True] * 3
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
    performance_applicable = dimension_applies(dimensions, "performance", project_type)
    performance_configured = bool(harness["quality"].get("performance_evidence", {}).get("command"))
    performance_result = (
        collect_backend_performance(root, harness) if performance_applicable and performance_configured else None
    )
    frontend_command = resolve_command(commands.get("performance", [])) if managed_runtime else []
    frontend_producer_ok = False
    if performance_applicable and project_type in {"frontend", "fullstack"} and frontend_command:
        (coverage_dir / "lighthouse.json").unlink(missing_ok=True)
        frontend_producer_ok = run_consumer(frontend_command).ok
    legacy_frontend_artifact = legacy_frontend_performance_artifact(project_type, coverage_dir)
    frontend_performance = (not frontend_command or frontend_producer_ok) and legacy_frontend_artifact
    performance = bool(performance_result and performance_result.ok) or frontend_performance
    if performance_result:
        score.evidence["performance"] = performance_result.evidence
    perf_max = weights["性能基线"]
    perf_score = (
        perf_max if not performance_applicable or performance else 0 if level == "L3" else round(perf_max * 0.5)
    )
    if performance_applicable and level == "L3" and not performance:
        score.hard_failures.append("L3 要求性能基线")
    performance_section = (
        performance_result.section
        if performance_result
        else f"- 适用: {performance_applicable}\n- 受控 producer: {bool(frontend_command)}"
        f"\n- producer 通过: {frontend_producer_ok}\n- 性能产物: {performance}"
    )
    score.add("性能基线", perf_score, performance_section)
    collect_action_evidence(score, root, harness, run_consumer, environment)


def run_with_managed_quality_runtime(
    root: Path,
    harness: dict[str, Any],
    consumer: Callable[[Mapping[str, str]], None],
) -> None:
    runtime: ManagedQualityRuntime | None = None
    previous_handlers: dict[int, Any] = {}
    primary_error: BaseException | None = None
    received_signal: QualityRuntimeSignal | None = None
    finalizing = False

    def handle_runtime_signal(signum: int, _frame: Any) -> None:
        nonlocal received_signal
        current = QualityRuntimeSignal(signum)
        if received_signal is not None and runtime is not None:
            runtime.force_kill = True
            return
        received_signal = current
        if finalizing:
            if runtime is not None:
                runtime.force_kill = True
            return
        raise current

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_runtime_signal)
    try:
        runtime = ManagedQualityRuntime.start(root, harness)
        consumer(runtime.wait_ready())
    except BaseException as exc:
        primary_error = exc
    finally:
        finalizing = True
        try:
            cleanup_failures = runtime.finalize() if runtime is not None else []
        except BaseException as exc:
            cleanup_failures = [f"质量 runtime finalizer 异常: {type(exc).__name__}"]
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    if primary_error is None and received_signal is not None:
        primary_error = received_signal
    if cleanup_failures:
        message = "; ".join(cleanup_failures)
        if isinstance(primary_error, QualityRuntimeSignal):
            primary_error.add_note(f"cleanup: {message}")
            raise primary_error
        if primary_error:
            raise ValueError(f"{primary_error}; cleanup: {message}") from primary_error
        raise ValueError(message)
    if primary_error:
        raise primary_error


def calculate(sprint: str, level: str, threshold: int, root: Path, coverage_dir: Path) -> Score:
    harness = load_harness_config()
    paths = HarnessPaths.detect(project=root)
    dimensions = harness["quality"]["dimensions"]
    project_type = harness["project"]["type"]
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
    integration_applicable = dimension_applies(dimensions, "integration", project_type)
    integration_files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and is_integration_test_path(root, path)
        and not {"node_modules", ".git", "coverage"}.intersection(path.parts)
    ]
    integration_command = resolve_command(commands.get("integration", [])) if integration_applicable else []
    integration_ok = bool(integration_command and try_run(integration_command, cwd=root).ok)
    integration = (
        weights["集成测试"]
        if not integration_applicable
        else (
            weights["集成测试"]
            if integration_ok
            else round(weights["集成测试"] * 0.5)
            if level == "L1" and not integration_files and not integration_command
            else 0
        )
    )
    if integration_applicable and level != "L1" and not integration_files and not integration_command:
        score.hard_failures.append(f"{level} 要求集成测试，但未找到入口")
    score.add(
        "集成测试",
        integration,
        f"- 适用: {integration_applicable}\n- 测试文件: {len(integration_files)}\n- 通过: {integration_ok}",
    )
    runtime_config = harness["quality"].get("runtime", {})
    runtime_enabled = bool(runtime_config.get("command")) and runtime_dimensions_apply(harness)
    if not runtime_enabled:
        calculate_runtime_dimensions(score, sprint, level, root, coverage_dir, harness, paths)
        return score

    def consume(environment: Mapping[str, str]) -> None:
        calculate_runtime_dimensions(
            score,
            sprint,
            level,
            root,
            coverage_dir,
            harness,
            paths,
            environment=environment,
            managed_runtime=True,
        )

    run_with_managed_quality_runtime(root, harness, consume)
    return score


def _evidence_markdown(score: Score) -> list[str]:
    action = score.evidence.get("action_evidence")
    if not isinstance(action, dict):
        return []
    rows = [
        "## Action Evidence",
        "",
        f"- Run ID: `{action.get('runId', '')}`",
        "",
        "### Live cases",
        "",
        "| Case | Spec | Status |",
        "| --- | --- | --- |",
    ]
    rows.extend(
        f"| {item.get('id', '')} | `{item.get('spec', '')}` | {item.get('status', '')} |"
        for item in action.get("liveCases", [])
        if isinstance(item, dict)
    )
    rows.extend(["", "### Bound artifacts", "", "| Path | MIME | SHA-256 |", "| --- | --- | --- |"])
    rows.extend(
        f"| `{item.get('path', '')}` | `{item.get('mimeType', '')}` | `{item.get('sha256', '')}` |"
        for item in action.get("artifacts", [])
        if isinstance(item, dict)
    )
    return rows + [""]


def _updated_report_index(score: Score, directory: Path, markdown: Path, generated_at: datetime) -> str:
    index = directory / "index.md"
    if index.exists():
        source = index.read_text(encoding="utf-8")
        if "| Sprint |" not in source or "| _(待生成)_ |" not in source:
            raise ValueError("测试报告 index 结构不可解析")
    else:
        source = "\n".join(
            [
                "# 测试质量报告索引",
                "",
                "| Sprint | 报告路径 | 质量评分 | 评定结论 | 执行日期 | 验证状态 |",
                "| --- | --- | ---: | --- | --- | --- |",
                "| _(待生成)_ | — | — | — | — | — |",
                "",
            ]
        )
    basename = markdown.name
    targets = [line for line in source.splitlines() if basename in line]
    if len(targets) > 1 or (targets and not targets[0].startswith("|")):
        raise ValueError("测试报告 index 目标条目重复或畸形")
    label = score.sprint.replace("sprint-", "Sprint ", 1)
    conclusion = "✅ 达标" if score.passed else "❌ 不达标"
    row = (
        f"| {label} | [{basename}]({basename}) | {score.total}/100 | {conclusion} | "
        f"{generated_at.date().isoformat()} | draft |"
    )
    if targets:
        return source.replace(targets[0], row)
    return source.replace("| _(待生成)_ |", f"{row}\n| _(待生成)_ |", 1)


def write_report(score: Score, directory: Path, *, source_commit: str | None = None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    base = quality_report_basename(score.sprint)
    markdown = directory / f"{base}.md"
    summary = directory / f"{base}.json"
    generated_at = datetime.now().astimezone()
    index_content = _updated_report_index(score, directory, markdown, generated_at)
    rows = "\n".join(f"| {label} | {value} | {maximum} |" for label, value, maximum in score.details)
    markdown.write_text(
        "\n".join(
            [
                f"# Sprint {score.sprint} 质量评分报告",
                "",
                f"- **层级**: {score.level}（CICD.md）",
                f"- **生成时间**: {generated_at.isoformat(timespec='seconds')}",
                f"- **阈值**: {score.threshold} 分",
                f"- **结果**: {'✅ 达标' if score.passed else '❌ 不达标'} ({score.total}/100)",
                f"- **硬门禁失败**: {'；'.join(score.hard_failures) if score.hard_failures else '无'}",
                f"- **源码提交**: {source_commit or '未绑定'}",
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
                *_evidence_markdown(score),
                f"**结果: {'✅ 达标' if score.passed else '❌ 不达标'}**",
                "",
            ]
        ),
        encoding="utf-8",
    )
    data = {
        "sprint": score.sprint,
        "level": score.level,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "threshold": score.threshold,
        "total": score.total,
        "max": 100,
        "passed": score.passed,
        "hard_failures": score.hard_failures,
        "source_commit": source_commit,
        "evidence": score.evidence,
        "details": [
            {"label": label, "score": value, "max": maximum, "weight": f"{maximum}%"}
            for label, value, maximum in score.details
        ],
    }
    summary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "index.md").write_text(index_content, encoding="utf-8")
    return markdown, summary


def library_source_commit(root: Path, sprint: str, report_dir: Path, coverage_dir: Path) -> str:
    commit = try_run(["git", "rev-parse", "HEAD"], cwd=root)
    if not commit.ok or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit.stdout.strip()):
        raise ValueError("无法解析 Library quality source commit")
    status = try_run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root)
    if not status.ok:
        raise ValueError("无法检查 Library quality worktree 状态")
    allowed_paths = {
        f"docs/exec-plans/active/{normalize_sprint_id(sprint)}.md",
    }
    allowed_prefixes = (
        ".harness/",
        "dist/",
        report_dir.resolve().relative_to(root.resolve()).as_posix().rstrip("/") + "/",
        coverage_dir.resolve().relative_to(root.resolve()).as_posix().rstrip("/") + "/",
    )
    dirty = []
    for line in status.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path not in allowed_paths and not path.startswith(allowed_prefixes):
            dirty.append(path)
    if dirty:
        raise ValueError("Library quality 要求源码已提交，worktree 仍有变更: " + ", ".join(dirty))
    return commit.stdout.strip()


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
    source_commit = None
    signal_exit_code: int | None = None
    try:
        if config["project"]["type"] == "library":
            source_commit = library_source_commit(Path.cwd(), sprint, args.report_dir, args.coverage_dir)
        score = calculate(sprint, args.level, threshold, Path.cwd(), args.coverage_dir)
    except (ValueError, QualityRuntimeSignal) as exc:
        if isinstance(exc, QualityRuntimeSignal):
            signal_exit_code = exc.exit_code
        dimensions = config["quality"]["dimensions"]
        weights = {value["label"]: value["weight"] for value in dimensions.values()}
        score = Score(sprint, args.level, threshold, weights, hard_failures=[str(exc)])
        for label in weights:
            score.add(label, 0, f"- Hard failure: {exc}" if label == "静态检查" else "- 未执行")
    report, summary = write_report(score, args.report_dir, source_commit=source_commit)
    artifacts = [report, summary, args.report_dir / "index.md"]
    evidence_artifact = config["quality"].get("action_evidence", {}).get("artifact", "")
    if evidence_artifact:
        artifacts.append(Path(evidence_artifact))
    print(
        json.dumps(
            {"score": score.total, "passed": score.passed, "artifacts": [str(path) for path in artifacts]},
            ensure_ascii=False,
        )
    )
    if signal_exit_code is not None:
        return signal_exit_code
    return 0 if score.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

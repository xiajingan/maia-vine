#!/usr/bin/env python3
"""Composable Harness verification runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from mai_harness.runtime.infrastructure.core.paths import PATHS, HarnessPaths
from mai_harness.runtime.infrastructure.core.process import ManagedProcess
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import load_environments_compat
from mai_harness.runtime.infrastructure.harness_config import load_harness_config, resolve_command_group
from mai_harness.runtime.infrastructure.local_runtime_env import parse_env_file_content
from mai_harness.runtime.infrastructure.technology_config import (
    load_technology_config,
    validate_technology_capabilities,
)
from mai_harness.runtime.infrastructure.utils import has_command, try_run


def config_defaults() -> dict:
    harness = load_harness_config()
    verification = harness["verification"]
    commands = harness["commands"]
    return {
        "API_URL": verification["api_url"],
        "WEB_URL": verification["web_url"],
        "HEALTH_ENDPOINT": verification["health_endpoint"],
        "READY_ENDPOINT": verification["ready_endpoint"],
        "EXECUTION_RUNTIME": verification["runtime"],
        "COMPOSE_FILE": verification["compose_file"],
        "DOCKER_REQUIRED_SERVICES": "",
        "DOCKER_BUILD_SERVICES": "",
        "APP_START_SERVICES": "",
        "MOCK_SERVICE_NAME": "",
        "STARTUP_CMD": commands["dev"],
        "STATIC_COMMANDS": resolve_command_group(harness, "static"),
        "STARTUP_WAIT": str(verification["startup_wait_seconds"]),
        "PREFLIGHT_CACHE_TTL_SECONDS": str(verification["preflight_cache_ttl_seconds"]),
        "SCREENSHOT_PATHS": verification["screenshot_paths"],
        "LOG_QUERY_CMD": "",
        "METRIC_QUERY_CMD": "",
        "LOG_VALIDATE_FIELDS": verification["log_validate_fields"],
        "ENV_FILE": verification["env_file"],
        "REQUIRED_ENV_VARS": verification["required_env_vars"],
        "DB_CHECK_CMD": "",
        "DB_MIN_TABLES": str(verification["db_min_tables"]),
        "EXTERNAL_CHECK_CMDS": "",
        "PROJECT_TYPE": harness["project"]["type"],
    }


def load_config(path: Path) -> dict:
    config = config_defaults()
    if not path.exists():
        return config
    if path.suffix == ".json":
        overrides = json.loads(path.read_text(encoding="utf-8"))
    else:
        overrides = parse_env_file_content(path.read_text(encoding="utf-8"))
    config.update(overrides)
    return config


@dataclass
class Verification:
    config: dict
    root: Path
    report_dir: Path
    results: list[str] = field(default_factory=list)
    failures: int = 0
    process: ManagedProcess | None = None

    def pass_(self, message: str) -> None:
        self.results.append(f"✅ {message}")
        print(f"✅ {message}")

    def warn(self, message: str) -> None:
        self.results.append(f"⚠️  {message}")
        print(f"⚠️  {message}")

    def fail(self, message: str) -> None:
        self.results.append(f"❌ {message}")
        self.failures += 1
        print(f"❌ {message}")

    def words(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        return value if isinstance(value, list) else str(value).split()

    def command(self, command: str, success: str, failure: str) -> bool:
        result = try_run(command, cwd=self.root)
        self.pass_(success) if result.ok else self.fail(failure)
        return result.ok

    def fingerprint(self, config_path: Path) -> dict:
        files = [
            config_path,
            self.root / self.config["ENV_FILE"],
            self.root / self.config["COMPOSE_FILE"],
            self.root / "package.json",
            self.root / "pnpm-lock.yaml",
        ]
        return {
            "config": str(config_path),
            "runtime": self.config["EXECUTION_RUNTIME"],
            "mtimes": {str(path): path.stat().st_mtime_ns if path.exists() else 0 for path in files},
        }

    def preflight(self, config_path: Path, skip_if_recent: int = 0) -> None:
        cache_path = self.root / ".harness/preflight.last-pass.json"
        state = StateStore(cache_path.parent)
        fingerprint = self.fingerprint(config_path)
        if skip_if_recent and cache_path.exists():
            try:
                cache = state.read_json(cache_path.name, {})
                if (
                    cache.get("fingerprint") == fingerprint
                    and time.time() - cache.get("timestamp", 0) <= skip_if_recent
                ):
                    self.pass_(f"Preflight: 最近 {skip_if_recent}s 内已通过且关键文件未变更，跳过")
                    return
            except ValueError:
                pass
        before = self.failures
        if (self.root / "config/harness.yml").is_file():
            paths = HarnessPaths.detect(project=self.root)
            try:
                harness = load_harness_config(force=True, path=self.root / "config/harness.yml")
                technology = load_technology_config(
                    path=self.root / "config/technology.yml",
                    defaults_path=paths.framework_config / "technology.defaults.yml",
                )
                if harness["project"]["mode"] != "control":
                    for error in validate_technology_capabilities(technology, harness, self.root):
                        self.fail(f"Technology: {error}")
            except (OSError, ValueError) as exc:
                self.fail(f"Technology: {exc}")
        warned = False
        if self.config["EXECUTION_RUNTIME"] == "docker":
            if not has_command("docker"):
                self.fail("Preflight: docker 不可用")
            else:
                compose = self.config["COMPOSE_FILE"]
                services = try_run(["docker", "compose", "-f", compose, "config", "--services"], cwd=self.root)
                defined = services.stdout.split() if services.ok else []
                required = self.words("DOCKER_REQUIRED_SERVICES")
                missing = [item for item in required if item not in defined]
                self.check(
                    bool(required) and not missing,
                    f"Preflight: Compose 已声明必需服务（{', '.join(required)}）",
                    f"Preflight: Compose 必需服务缺失或未配置（{', '.join(missing)}）",
                )
                mock = self.config["MOCK_SERVICE_NAME"].strip()
                self.check(
                    mock == "NONE" or bool(mock and mock in defined),
                    "Preflight: Mock 服务声明有效",
                    "Preflight: MOCK_SERVICE_NAME 未声明或服务不存在",
                )
                ps = try_run(
                    ["docker", "compose", "-f", compose, "ps", "--format", "{{.Name}} {{.Status}}"], cwd=self.root
                )
                lines = [line for line in ps.stdout.splitlines() if line.strip()] if ps.ok else []
                if not lines:
                    self.warn("Preflight: 当前无运行中的容器")
                    warned = True
                else:
                    self.check(
                        all(re.search(r"healthy|running", line, re.I) for line in lines),
                        "Preflight: Docker 服务健康",
                        "Preflight: Docker 服务异常",
                    )
        env_file = self.root / self.config["ENV_FILE"]
        if self.words("REQUIRED_ENV_VARS"):
            if not env_file.exists():
                self.fail(f"Preflight: {env_file} 不存在")
            else:
                values = parse_env_file_content(env_file.read_text(encoding="utf-8"))
                missing = [
                    key for key in self.words("REQUIRED_ENV_VARS") if not values.get(key) and not os.environ.get(key)
                ]
                self.check(
                    not missing, "Preflight: 必需环境变量已配置", f"Preflight: 环境变量缺失 → {' '.join(missing)}"
                )
        if self.config["DB_CHECK_CMD"]:
            result = try_run(self.config["DB_CHECK_CMD"], cwd=self.root)
            try:
                tables = int(result.stdout.strip())
            except ValueError:
                tables = 0
            minimum = int(self.config["DB_MIN_TABLES"] or 0)
            self.check(
                result.ok and tables >= minimum,
                f"Preflight: 数据库已初始化（{tables} 张表）",
                f"Preflight: 数据库表不足（{tables}/{minimum}）",
            )
        for command in [item.strip() for item in self.config["EXTERNAL_CHECK_CMDS"].split("|") if item.strip()]:
            self.command(
                command, f"Preflight: 外部服务检查通过 — {command[:60]}", f"Preflight: 外部服务不可达 — {command[:60]}"
            )
        if self.failures == before and not warned:
            state.write_json(cache_path.name, {"timestamp": time.time(), "fingerprint": fingerprint})

    def check(self, condition: bool, success: str, failure: str) -> None:
        self.pass_(success) if condition else self.fail(failure)

    def docker_up(self) -> None:
        if self.config["EXECUTION_RUNTIME"] != "docker":
            self.fail("Docker-up: EXECUTION_RUNTIME != docker")
            return
        compose = self.config["COMPOSE_FILE"]
        start = self.words("APP_START_SERVICES") or self.words("DOCKER_REQUIRED_SERVICES")
        build = self.words("DOCKER_BUILD_SERVICES") or start
        if not has_command("docker") or not start:
            self.fail("Docker-up: docker 不可用或启动服务未配置")
            return
        try_run(["docker", "compose", "-f", compose, "down"], cwd=self.root)
        if build and not self.command(
            ["docker", "compose", "-f", compose, "build", *build], "Docker-up: 镜像构建完成", "Docker-up: 镜像构建失败"
        ):
            return
        self.command(
            ["docker", "compose", "-f", compose, "up", "-d", "--wait", *start],
            "Docker-up: 服务已启动并 healthy",
            "Docker-up: 启动失败或健康检查超时",
        )

    def docker_down(self, purge: bool = False) -> None:
        if not has_command("docker"):
            self.warn("Docker-down: docker 不可用")
            return
        command = ["docker", "compose", "-f", self.config["COMPOSE_FILE"], "down"] + (["-v"] if purge else [])
        self.command(command, "Docker-down: 清理完成", "Docker-down: 清理失败")

    def static(self) -> None:
        for command in self.config["STATIC_COMMANDS"]:
            self.command(command, f"Static: {command}", f"Static: {command}")

    def health(self, auto_start: bool = True) -> None:
        if auto_start and self.config["EXECUTION_RUNTIME"] == "docker":
            self.docker_up()
        elif auto_start and self.config["STARTUP_CMD"]:
            self.process = ManagedProcess.start(self.config["STARTUP_CMD"], cwd=self.root)
            self.report_dir.mkdir(parents=True, exist_ok=True)
            (self.report_dir / ".pids").write_text(f"{self.process.pid}\n", encoding="utf-8")
            time.sleep(int(self.config["STARTUP_WAIT"] or 15))
        for label, url in (
            ("API health", self.config["API_URL"] + self.config["HEALTH_ENDPOINT"]),
            ("API ready", self.config["API_URL"] + self.config["READY_ENDPOINT"]),
            ("Web 首页", self.config["WEB_URL"]),
        ):
            if not url:
                continue
            if label == "API ready" and not self.config["READY_ENDPOINT"]:
                continue
            started = time.monotonic()
            try:
                with urlopen(url, timeout=10) as response:
                    status = response.status
            except (URLError, TimeoutError):
                status = 0
            self.check(
                status == 200,
                f"Health: {label} → 200 ({time.monotonic() - started:.3f}s)",
                f"Health: {label} → {status} (期望 200)",
            )

    def screenshot(self) -> None:
        if self.config["PROJECT_TYPE"] == "backend" or not self.config["WEB_URL"]:
            self.warn("Screenshot: backend 工程无 Web 入口，跳过")
            return
        if not has_command("npx"):
            self.warn("Screenshot: Playwright CLI 不可用")
            return
        directory = self.report_dir / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        for route in self.words("SCREENSHOT_PATHS"):
            output = directory / (re.sub(r"[^a-zA-Z0-9]", "_", route) + ".png")
            self.command(
                [
                    "npx",
                    "playwright",
                    "screenshot",
                    "--browser",
                    "chromium",
                    self.config["WEB_URL"] + route,
                    str(output),
                ],
                f"Screenshot: {route} → {output}",
                f"Screenshot: {route} 截图失败",
            )

    def logs(self) -> None:
        command = self.config["LOG_QUERY_CMD"]
        if not command:
            self.warn("Logs: LOG_QUERY_CMD 未配置")
            return
        result = try_run(command, cwd=self.root)
        output = result.stdout
        if not output.strip():
            self.fail("Logs: 查询无输出")
            return
        self.pass_(f"Logs: 查询有输出 ({len(output)} bytes)")
        sample = output.splitlines()[:5]
        try:
            all(json.loads(line) for line in sample if line.strip())
            self.pass_("Logs: JSON 结构化格式")
        except json.JSONDecodeError:
            self.warn("Logs: 非 JSON 格式")
        self.check(
            not re.search(r"(password|secret|api.?key|token)[\"']\s*:\s*[\"'][^\"']{8,}", output, re.I),
            "Logs: 无明显敏感信息泄露",
            "Logs: 检测到疑似敏感信息泄露",
        )
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / "log-sample.txt").write_text("\n".join(output.splitlines()[:20]) + "\n", encoding="utf-8")

    def metrics(self) -> None:
        command = self.config["METRIC_QUERY_CMD"]
        if not command:
            self.warn("Metrics: METRIC_QUERY_CMD 未配置")
            return
        result = try_run(command, cwd=self.root)
        if result.stdout.strip():
            self.pass_("Metrics: 查询有输出")
            self.report_dir.mkdir(parents=True, exist_ok=True)
            (self.report_dir / "metrics-sample.txt").write_text(result.stdout, encoding="utf-8")
        else:
            self.fail("Metrics: 查询无输出")

    def profile_check(self) -> None:
        self.check(
            any(
                (self.root / name).exists() for name in ("PROJECT_RULES.md", "package.json", "pyproject.toml", "go.mod")
            ),
            "项目根标识文件存在",
            "未发现项目根标识文件",
        )
        try:
            environments = load_environments_compat().get("environments", {})
        except Exception as exc:
            self.fail(f"部署配置解析失败: {exc}")
            return
        self.check(
            all(name in environments for name in ("test", "prod")),
            "部署环境定义完整（test/prod）",
            "部署环境定义缺少 test/prod",
        )
        profile = os.environ.get("HARNESS_VERIFY_PROFILE")
        targets = [profile] if profile else ["test", "prod"]
        for name in targets:
            entry = environments.get(name)
            compose = self.root / entry.get("compose_file", "") if entry else Path("")
            if not entry or not compose.exists():
                self.fail(f"{name}: compose_file 不存在或不可读")
                continue
            result = try_run(
                ["docker", "compose", "-f", str(compose), "config", "--services"],
                cwd=self.root,
                env={
                    "QWCHAT_API_IMAGE_REPOSITORY": "invalid/api",
                    "QWCHAT_WEB_IMAGE_REPOSITORY": "invalid/web",
                    "QWCHAT_IMAGE_TAG": "profile-check",
                },
            )
            services = result.stdout.split() if result.ok else []
            self.check(
                bool(services) and (name == "dev" or not any("mock" in service.lower() for service in services)),
                f"{name}: 部署资产可解析（{len(services)} 个服务）",
                f"{name}: 部署资产不可解析或包含 mock",
            )

    def report(self, config_path: Path) -> Path:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        target = self.report_dir / f"verify-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
        conclusion = "✅ 全部通过" if not self.failures else f"❌ {self.failures} 项失败"
        target.write_text(
            "\n".join(
                [
                    "# 验证报告",
                    "",
                    f"- 时间: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                    f"- 配置: {config_path}",
                    f"- 结论: {conclusion}",
                    "",
                    "## 明细",
                    "",
                    *[f"- {item}" for item in self.results],
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"报告: {target}")
        return target

    def cleanup(self) -> None:
        if self.process:
            self.process.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phases", nargs="*", default=["all"])
    parser.add_argument("--config", type=Path, default=PATHS.config / "verify.config.sh")
    parser.add_argument("--report-dir", type=Path, default=Path(".harness/verify-reports"))
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--skip-if-recent", type=int, default=0)
    parser.add_argument("--purge", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    verification = Verification(load_config(args.config), root, args.report_dir)
    phases = args.phases or ["all"]

    def should_run(phase: str) -> bool:
        return phase in phases or ("all" in phases and phase not in {"docker-up", "docker-down"})

    try:
        if should_run("preflight"):
            verification.preflight(args.config, args.skip_if_recent)
        if should_run("docker-up"):
            verification.docker_up()
        if should_run("docker-down"):
            verification.docker_down(args.purge)
        if should_run("static"):
            verification.static()
        if should_run("health"):
            verification.health(not args.no_start)
        if should_run("screenshot"):
            verification.screenshot()
        if should_run("logs"):
            verification.logs()
        if should_run("metrics"):
            verification.metrics()
        if should_run("profile-check"):
            verification.profile_check()
        verification.report(args.config)
        return min(verification.failures, 125)
    finally:
        verification.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

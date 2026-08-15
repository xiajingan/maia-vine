#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.deploy_config import load_environments_compat
from mai_harness.runtime.infrastructure.utils import err, info, ok

IGNORES = {".git", "node_modules", ".worktrees", "dist", "build", "state", ".venv", ".venv312", "__pycache__"}
PATTERNS = {
    "api-key": re.compile(r"(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[\"']?([A-Za-z0-9+/=_-]{16,})", re.I),
    "aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
}


def candidates(root: Path, relative_paths: list[str] | None = None):
    paths = (root / path for path in relative_paths) if relative_paths is not None else root.rglob("*")
    for path in paths:
        if (
            not path.is_file()
            or IGNORES.intersection(path.parts)
            or path.suffix.lower()
            in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".pdf",
                ".zip",
                ".tar",
                ".gz",
                ".ico",
                ".woff",
                ".woff2",
                ".ttf",
                ".mp4",
                ".lock",
                ".lockb",
            }
            or path.name.endswith(".example")
            or {"fixtures", "__fixtures__"}.intersection(path.parts)
        ):
            continue
        yield path


def staged_paths(root: Path) -> list[str]:
    result = execute(
        CommandSpec.argv_command(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root)
    )
    if not result.ok:
        raise RuntimeError(f"读取 Git 暂存区失败：{result.stderr.strip()}")
    return [path for path in result.stdout.split("\0") if path]


def scan(root: Path, *, staged: bool = False) -> int:
    hits = []
    relative_paths = staged_paths(root) if staged else None
    for path in candidates(root, relative_paths):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                sample = match.group(0)
                if re.search(r"<.+>|\$\{.+}|x{8,}|placeholder|EXAMPLE", sample, re.I):
                    continue
                hits.append((path, text.count("\n", 0, match.start()) + 1, kind, sample[:80]))
    if hits:
        err(f"发现 {len(hits)} 处疑似 secret 字面值：")
        for path, line, kind, sample in hits:
            print(f"  {path}:{line} [{kind}] {sample}")
        return 1
    ok("未发现疑似 secret")
    return 0


def check_cross_env() -> int:
    path = Path(".env.dev.example")
    if not path.exists():
        info(f"{path} 不存在；跳过跨环境名称检查")
        return 0
    errors = [
        f"{name} 出现在 dev 模板中，但前缀不是 HARNESS_DEV_"
        for name in re.findall(r"^(HARNESS_[A-Z0-9_]+)=", path.read_text(encoding="utf-8"), re.M)
        if not name.startswith("HARNESS_DEV_")
    ]
    if (PATHS.project_config / "deploy.yml").exists():
        environments = load_environments_compat().get("environments", {})
        for env in ("test", "prod"):
            source = environments.get(env, {}).get("secrets_source")
            if source and not re.search(r":test|:prod|local-env-file|vault|\.harness/secrets/", source):
                errors.append(f"environments.{env}.secrets_source 格式可疑：{source}")
    if errors:
        err("跨环境检查失败：\n  - " + "\n  - ".join(errors))
        return 1
    ok("跨环境 secret 命名规范通过")
    return 0


def vcs_cli_scan(root: Path) -> int:
    allowed = (
        "workflows/",
        "src/mai_harness/runtime/",
    )
    pattern = re.compile(r"\b(gh|glab)\s+(pr|mr|api|repo)\b|\bgit\s+push\b")
    hits = []
    for path in candidates(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() not in {".yaml", ".yml", ".sh", ".bash"} or any(item in relative for item in allowed):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if match := pattern.search(line):
                hits.append((relative, number, match.group(0)))
    if hits:
        err(f"发现 {len(hits)} 处直接 VCS CLI 调用（请使用 harness pr-adapter）：")
        for path, line, sample in hits:
            print(f"  {path}:{line} → {sample}")
        return 1
    ok("未发现直连 VCS CLI 调用")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scan", "check-cross-env", "vcs-cli-scan"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--staged", action="store_true", help="仅扫描 Git 暂存区中的文件")
    args = parser.parse_args()
    return (
        scan(args.root, staged=args.staged)
        if args.command == "scan"
        else check_cross_env()
        if args.command == "check-cross-env"
        else vcs_cli_scan(args.root)
    )


if __name__ == "__main__":
    raise SystemExit(main())

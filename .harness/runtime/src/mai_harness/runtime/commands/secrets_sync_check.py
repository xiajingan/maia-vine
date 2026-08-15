#!/usr/bin/env python3

from mai_harness.runtime.application.required_secrets import resolve_required_secrets
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.deploy_config import load_environments_compat
from mai_harness.runtime.infrastructure.secrets_file import expected_secrets_source, load_secrets_file_snapshot
from mai_harness.runtime.infrastructure.utils import err, info, ok


def main() -> int:
    config_path = PATHS.project_config / "deploy.yml"
    if not config_path.exists():
        raise SystemExit("config/deploy.yml 不存在")
    problems = 0
    for env, entry in load_environments_compat().get("environments", {}).items():
        if not entry.get("enabled"):
            continue
        expected = expected_secrets_source(env)
        if entry.get("secrets_source") != expected:
            err(f"environments.{env}.secrets_source 必须为 {expected}")
            problems += 1
        snapshot = load_secrets_file_snapshot(env, resolve_required_secrets(env, entry))
        if snapshot["error"]:
            err(f"{snapshot['path']} 无法加载：{snapshot['error']}")
            problems += 1
        elif not snapshot["exists"]:
            info(f"{expected} 尚不存在（允许）；promote-prep 将自动生成模板")
    if problems:
        err(f"secrets-sync-check 发现 {problems} 个问题")
        return 1
    ok(f"secrets-sync-check 通过：{config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

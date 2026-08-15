#!/usr/bin/env python3
"""Migrate legacy environment/build configuration into config/deploy.yml."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.deploy_config import merge_legacy_config
from mai_harness.runtime.infrastructure.utils import load_yaml, write_yaml

LEGACY_FILES = ("config/environments.yml", "config/build-targets.yml", "config/build-targets.example.yml")


def migrate(root: Path, *, keep_legacy: bool = False) -> tuple[Path | None, Path | None]:
    target = root / "config/deploy.yml"
    if target.exists():
        return None, None
    environments = root / "config/environments.yml"
    if not environments.exists():
        raise FileNotFoundError("config/environments.yml 不存在，无法迁移")
    build = root / "config/build-targets.yml"
    config = merge_legacy_config(load_yaml(environments), load_yaml(build) if build.exists() else None)
    write_yaml(target, config)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds").replace(":", "-")
    backup = root / ".harness/migrations" / f"deploy-config-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for relative in LEGACY_FILES:
        source = root / relative
        if not source.exists():
            continue
        destination = backup / relative.replace("/", "__")
        shutil.copy2(source, destination) if keep_legacy else shutil.move(source, destination)
    return target, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-legacy", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        target, backup = migrate(args.root, keep_legacy=args.keep_legacy)
    except (OSError, ValueError) as exc:
        print(f"[migrate-deploy-config] 失败: {exc}")
        return 1
    if target is None:
        print("[migrate-deploy-config] config/deploy.yml 已存在，跳过")
    else:
        print(f"[migrate-deploy-config] {target} 已生成；旧配置已备份到 {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

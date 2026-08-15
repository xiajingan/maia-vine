#!/usr/bin/env python3
"""Validate reversible release SQL migration assets."""

import argparse
import re
import sys
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.utils import try_run

NAME = re.compile(r"^(\d{3}-[a-z0-9-]+)\.(up|down)\.sql$")


def validate(directory: Path, actions: set[str]) -> list[str]:
    if not directory.exists():
        return [f"目录不存在: {directory}"]
    files = sorted(directory.glob("*.sql"))
    errors = []
    if "name" in actions:
        errors += [f"命名不规范: {file.name}" for file in files if not NAME.match(file.name)]
    if "pair" in actions:
        up = {match.group(1) for file in files if (match := NAME.match(file.name)) and match.group(2) == "up"}
        down = {match.group(1) for file in files if (match := NAME.match(file.name)) and match.group(2) == "down"}
        errors += [f"缺少 down: {name}" for name in sorted(up - down)] + [
            f"孤立 down: {name}" for name in sorted(down - up)
        ]
    if "dry-down" in actions:
        for file in files:
            if not file.name.endswith(".down.sql"):
                continue
            text = file.read_text(encoding="utf-8")
            if not re.search(r"(^|\s)(BEGIN|START\s+TRANSACTION)\s*;", text, re.I) or not re.search(
                r"(^|\s)(COMMIT|ROLLBACK)\s*;", text, re.I
            ):
                errors.append(f"{file.name}: 缺少事务包裹")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pair", "name", "sign", "dry-down", "idempotency", "rehearse", "all"))
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    actions = {"pair", "name", "dry-down"} if args.action == "all" else {args.action}
    errors = validate(args.release_dir, actions)
    if args.action in {"sign", "all"} and not errors:
        manifest = args.release_dir / "manifest.yml"
        if not manifest.exists():
            errors.append(f"未找到 {manifest}")
        else:
            sha = try_run(["git", "rev-parse", "HEAD"]).stdout.strip()
            text = manifest.read_text()
            text = (
                re.sub(r"^signature:.*$", f'signature: "{sha}"', text, flags=re.M)
                if re.search(r"^signature:", text, re.M)
                else text.rstrip() + f'\nsignature: "{sha}"\n'
            )
            manifest.write_text(text, encoding="utf-8")
    if args.action in {"idempotency", "rehearse"}:
        hook = Path("scripts/migration_runner.py")
        if hook.exists():
            return execute(
                CommandSpec.argv_command([sys.executable, str(hook), args.action, str(args.release_dir)])
            ).returncode
        print("⚠️  项目未提供 scripts/migration_runner.py，跳过深度校验")
    for error in errors:
        print(f"❌ {error}")
    if not errors:
        print(f"✅ migration {args.action} 通过")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

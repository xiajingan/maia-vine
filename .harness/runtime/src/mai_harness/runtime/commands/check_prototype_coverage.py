#!/usr/bin/env python3
"""Ensure every Sprint HTML prototype is registered by a UI contract."""

import argparse
from pathlib import Path

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.ui_contracts import load_contracts


def missing_prototypes(root: Path, sprint: str, contracts: list[dict]) -> list[str]:
    sprint_name = sprint if sprint.startswith("sprint-") else f"sprint-{sprint}"
    directory = root / "docs/design-docs/prototypes" / sprint_name
    prototypes = (
        [path.relative_to(root).as_posix() for path in directory.glob("*.html") if path.name != "index.html"]
        if directory.exists()
        else []
    )
    registered = {str(item.get("prototype", {}).get("path", "")).replace("\\", "/") for item in contracts}
    return [path for path in prototypes if path not in registered]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--file", type=Path, default=PATHS.rules / "ui-contracts.yml")
    args = parser.parse_args()
    root = Path.cwd()
    sprint_name = args.sprint if args.sprint.startswith("sprint-") else f"sprint-{args.sprint}"
    directory = root / "docs/design-docs/prototypes" / sprint_name
    if not directory.exists():
        print(f"✅ Sprint {args.sprint} 无原型目录，跳过")
        return 0
    try:
        plan = load_contracts(args.file, args.sprint)
    except (OSError, ValueError) as exc:
        print(f"❌ {exc}")
        return 1
    html_count = len([path for path in directory.glob("*.html") if path.name != "index.html"])
    if html_count and not plan["required"]:
        print(f"❌ 原型存在但 required=false（{plan['reason']}）")
        return 1
    missing = missing_prototypes(root, args.sprint, plan["contracts"])
    for path in missing:
        print(f"❌ 未注册原型: {path}")
    if not missing:
        print(f"✅ Sprint {args.sprint} 原型 100% 覆盖（{html_count}/{html_count}）")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

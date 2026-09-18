"""Migrate or roll back project-owned document scope registries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.document_registry_migration import (
    migrate_design_policy,
    migrate_document_registries,
    rollback_document_registry_migrations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--mapping", type=Path, help="旧简单索引逐文件语义确认 YAML（必须位于当前工程内）")
    parser.add_argument("--design-policy", type=Path, help="将一个已激活 Sprint 显式迁移到设计治理 v2")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        if args.design_policy:
            if args.rollback or args.mapping:
                parser.error("设计策略迁移不能与索引迁移/回滚混用")
            result = migrate_design_policy(root, args.design_policy.resolve(), args.reason)
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
            return 0
        results = (
            rollback_document_registry_migrations(root)
            if args.rollback
            else migrate_document_registries(root, mapping_path=args.mapping)
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

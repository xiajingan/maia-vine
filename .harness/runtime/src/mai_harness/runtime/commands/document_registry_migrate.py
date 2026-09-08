"""Migrate or roll back project-owned document scope registries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.document_registry_migration import (
    migrate_document_registries,
    rollback_document_registry_migrations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--mapping", type=Path, help="旧简单索引逐文件语义确认 YAML（必须位于当前工程内）")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
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

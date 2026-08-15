#!/usr/bin/env python3
"""Promote an immutable registry image or saved artifact without rebuilding."""

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.state_store import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--to", required=True)
    parser.add_argument("--registry", default=os.environ.get("REGISTRY", ""))
    parser.add_argument("--from-tar", type=Path)
    parser.add_argument("--to-tar", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    mode = os.environ.get("HARNESS_DELIVERY_MODE", "artifact" if args.from_tar else "registry")
    if mode == "artifact":
        if not args.from_tar or not args.from_tar.exists():
            parser.error("artifact 模式需要存在的 --from-tar")
        target = args.to_tar or args.from_tar.with_name(args.from_tar.name.replace(args.source, args.to))
        sha = hashlib.sha256(args.from_tar.read_bytes()).hexdigest()
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.from_tar, target)
        evidence = {"mode": mode, "from": args.source, "to": args.to, "artifact": str(target), "artifact_sha256": sha}
    else:
        if not args.registry:
            parser.error("registry 模式需要 --registry/REGISTRY")
        source, target = f"{args.registry}:{args.source}", f"{args.registry}:{args.to}"
        command = ["docker", "buildx", "imagetools", "create", "-t", target, source]
        outcome = None if args.dry_run else execute(CommandSpec.argv_command(command))
        if outcome and not outcome.ok:
            print(f"❌ {outcome.stderr}")
            return outcome.returncode
        evidence = {"mode": mode, "from": source, "to": target}
    evidence["promoted_at"] = datetime.now(UTC).isoformat()
    StateStore(Path(".harness/state")).write_json(f"image-promote-{args.to}.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

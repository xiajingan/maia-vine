#!/usr/bin/env python3
"""Build Sprint image targets and persist immutable evidence state."""

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.application.deployment_candidate import validate_deployment_candidate
from mai_harness.runtime.domain.sprint_context import header_field
from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute, harness_command
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import load_build_targets_compat, load_deploy_config
from mai_harness.runtime.infrastructure.utils import try_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--target", default="all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path.cwd()
    head = try_run(("git", "rev-parse", "HEAD"), cwd=root)
    source_commit = head.stdout.strip() if head.ok else ""
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        parser.error("build-image 必须在可解析完整 HEAD commit 的 Git 工作区执行")
    candidate_commit = source_commit
    source_signoffs: list[dict[str, str]] = []
    plan = next(
        (
            path
            for directory in ("active", "completed")
            if (path := root / "docs/exec-plans" / directory / f"{Path(args.sprint).name}.md").is_file()
        ),
        None,
    )
    if plan:
        content = plan.read_text(encoding="utf-8")
        if header_field(content, "sprint_type") == "deploy-sprint-test":
            validation = validate_deployment_candidate(
                root,
                content,
                source_field="source_sprints",
                candidate_field="base_sha",
                require_head_match=True,
                require_source_ancestry=True,
            )
            if validation.errors:
                for error in validation.errors:
                    print(f"❌ {error}")
                return 1
            candidate_commit = validation.candidate_commit
            source_signoffs = validation.source_signoffs
    build = load_build_targets_compat()
    deploy = load_deploy_config()
    if not build.get("targets"):
        parser.error("config/deploy.yml.build.targets 为空")
    tag = source_commit
    meta = root / f".harness/state/build-meta-{args.sprint}-{args.target}.json"
    modes = [
        entry.get("delivery_mode") or entry.get("registry_mode")
        for entry in deploy.get("environments", {}).values()
        if entry.get("delivery_mode") or entry.get("registry_mode")
    ]
    delivery = os.environ.get("HARNESS_DELIVERY_MODE") or (modes[0] if modes else "artifact")
    command = (
        [
            *harness_command("build-artifact"),
            "--target",
            args.target,
            "--tag",
            tag,
            "--meta-out",
            str(meta),
        ]
        + (["--save-tar"] if delivery == "artifact" else ["--push"])
        + (["--dry-run"] if args.dry_run else [])
    )
    outcome = execute(CommandSpec.argv_command(command, cwd=root))
    payload = json.loads(meta.read_text()) if outcome.ok and meta.exists() else {"images": []}
    state = {
        "sprint": args.sprint,
        "source_commit": source_commit,
        "candidate_commit": candidate_commit,
        "source_signoffs": source_signoffs,
        "tag": tag,
        "target": args.target,
        "success": outcome.ok,
        "artifact_dir": build.get("artifact_dir", ".harness/images"),
        "meta_file": str(meta),
        "images": payload["images"],
        "ts": datetime.now(UTC).isoformat(),
    }
    target = StateStore(root / ".harness/state").write_json(f"build-image-{args.sprint}.json", state)
    print(f"{'✅' if outcome.ok else '❌'} build-image: {target}")
    return outcome.returncode


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build configured Docker targets and emit immutable image/artifact evidence."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.deploy_config import load_build_targets_compat
from mai_harness.runtime.infrastructure.harness_config import load_package_document


def image_repo(config: dict) -> str:
    if os.environ.get("REGISTRY"):
        return os.environ["REGISTRY"]
    if config.get("image_repo"):
        return config["image_repo"]
    package, errors = load_package_document(Path("package.json"))
    name = package.get("name", "")
    return re.sub(r"^@[^/]+/", "", name) if not errors and isinstance(name, str) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=os.environ.get("IMAGE_TAG"))
    parser.add_argument("--target", default="all")
    parser.add_argument("--platform")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--save-tar", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--meta-out", type=Path, default=Path("image-meta.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.tag:
        parser.error("--tag 或 IMAGE_TAG 必填")
    config = load_build_targets_compat()
    targets = config.get("targets", {})
    selected = targets if args.target == "all" else {args.target: targets.get(args.target)}
    if not selected or any(value is None for value in selected.values()):
        parser.error(f"未知 target: {args.target}")
    repo = image_repo(config)
    if not repo:
        parser.error("image_repo 无法解析")
    images = []
    for name, definition in selected.items():
        dockerfile = Path(definition.get("dockerfile", ""))
        if not dockerfile.exists():
            parser.error(f"targets.{name}.dockerfile 不存在: {dockerfile}")
        ref = f"{repo}-{name}:{args.tag}"
        platform = (
            args.platform
            or os.environ.get("HARNESS_BUILD_PLATFORM")
            or ",".join(definition.get("platforms", []))
            or definition.get("platform")
            or config.get("default_platform", "linux/amd64")
        )
        digest = ""
        artifact = ""
        artifact_sha = ""
        if not args.skip_build:
            with tempfile.NamedTemporaryFile(suffix=".json") as metadata:
                command = [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    platform,
                    "-f",
                    str(dockerfile),
                    "-t",
                    ref,
                    "--metadata-file",
                    metadata.name,
                    "--push" if args.push else "--load",
                    definition.get("context", "."),
                ]
                print(" ".join(command))
                if not args.dry_run:
                    outcome = execute(CommandSpec.argv_command(command, cwd=Path.cwd()))
                    if not outcome.ok:
                        print(f"❌ {outcome.stderr or outcome.stdout}")
                        return outcome.returncode
                    try:
                        meta = json.loads(Path(metadata.name).read_text())
                        digest = meta.get("containerimage.digest") or meta.get("containerimage.config.digest", "")
                    except (OSError, json.JSONDecodeError):
                        pass
                    if args.push and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                        print(f"❌ {name}: push 完成但缺少不可变 digest")
                        return 1
        if args.save_tar:
            directory = args.out or Path(
                os.environ.get("HARNESS_ARTIFACT_DIR", config.get("artifact_dir", ".harness/images"))
            )
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{repo.replace('/', '_')}-{name}-{args.tag}.tar"
            artifact = str(target)
            if not args.dry_run:
                outcome = execute(CommandSpec.argv_command(["docker", "save", "-o", str(target), ref]))
                if not outcome.ok:
                    print(f"❌ {outcome.stderr or outcome.stdout}")
                    return outcome.returncode
                artifact_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        images.append(
            {
                "target": name,
                "ref": ref,
                "digest": digest,
                "artifact": artifact,
                "artifactSha256": artifact_sha,
                "platforms": platform.split(","),
            }
        )
    payload = {"tag": args.tag, "generated_at": datetime.now(UTC).isoformat(), "images": images}
    args.meta_out.parent.mkdir(parents=True, exist_ok=True)
    args.meta_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

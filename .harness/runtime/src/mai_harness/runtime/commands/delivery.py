"""Managed Delivery Manifest publish port."""

from __future__ import annotations

import argparse
from pathlib import Path

from mai_harness.runtime.application.collaboration import publish_delivery
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.manifest import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("manifest", type=Path)
    args = parser.parse_args()
    config = load_harness_config()
    if config["project"]["mode"] != "managed":
        parser.error("delivery 命令仅允许 managed 模式")
    print(publish_delivery(PATHS.project, config["management"]["deliveries_dir"], load_manifest(args.manifest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Execute configured Control system integration/E2E commands with release evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mai_harness.runtime.application.control import run_test_integration
from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    config = load_harness_config()
    if config["project"]["mode"] != "control":
        parser.error("test-integration 仅允许 control 模式")
    commands = config.get("control", {}).get("integration_commands", [])
    result = run_test_integration(
        args.manifest,
        commands,
        project_root=PATHS.project,
        state_root=PATHS.state,
    )
    result["manifest"] = str(args.manifest)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "test-verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())

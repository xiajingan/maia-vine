"""Validate the configured, requested, and installed Harness engineering mode."""

from __future__ import annotations

import argparse

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.mode_state import assert_mode_consistent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("standalone", "managed", "control"))
    args = parser.parse_args()
    config = load_harness_config()
    assert_mode_consistent(PATHS.project, config["project"]["mode"], args.mode)
    print(f"PASS: Harness mode={config['project']['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

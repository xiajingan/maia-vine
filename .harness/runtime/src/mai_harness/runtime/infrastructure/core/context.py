"""Explicit immutable runtime context for Harness application services."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import HarnessPaths
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.deploy_config import load_deploy_config
from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.technology_config import load_technology_config


@dataclass(frozen=True)
class HarnessContext:
    root: Path
    harness: Mapping[str, Any]
    technology: Mapping[str, Any]
    deploy: Mapping[str, Any]
    environment: Mapping[str, str]
    automation_state: StateStore
    pipeline_state: StateStore

    @classmethod
    def load(cls, root: Path | None = None, environment: Mapping[str, str] | None = None) -> HarnessContext:
        project_root = (root or Path.cwd()).resolve()
        paths = HarnessPaths.detect(project=project_root)
        return cls(
            project_root,
            load_harness_config(
                force=True,
                path=paths.project_config / "harness.yml",
                defaults_path=paths.framework_config / "harness.defaults.yml",
            ),
            load_technology_config(
                path=paths.project_config / "technology.yml",
                defaults_path=paths.framework_config / "technology.defaults.yml",
            ),
            load_deploy_config(force=True, path=paths.config / "deploy.yml"),
            dict(environment or os.environ),
            StateStore(project_root / ".harness/automation"),
            StateStore(project_root / ".harness/pipeline"),
        )

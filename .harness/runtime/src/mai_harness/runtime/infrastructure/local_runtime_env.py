"""Load project-local runtime environment files without shell evaluation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import HarnessPaths


def parse_env_file_content(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", raw)
        if not match:
            continue
        value = match.group(2)
        if value.startswith(('="', "='")):
            value = value[1:]
        quote = value[0] if value.startswith(('"', "'")) else ""
        while quote and not value.endswith(quote) and index < len(lines):
            value += "\n" + lines[index]
            index += 1
        if quote and value.startswith(quote) and value.endswith(quote):
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def resolve_local_runtime_env_files(root: Path | None = None, env: dict[str, str] | None = None) -> list[Path]:
    root = root or Path.cwd()
    environment = env or os.environ
    candidates: list[str] = []
    if environment.get("HARNESS_RUNTIME_ENV_FILE", "").strip():
        candidates.append(environment["HARNESS_RUNTIME_ENV_FILE"].strip())
    verify = HarnessPaths.detect(project=root).config / "verify.config.sh"
    if verify.exists():
        match = re.search(r'^ENV_FILE=(?:"([^"]+)"|\'([^\']+)\'|([^\n#]+))', verify.read_text(encoding="utf-8"), re.M)
        if match:
            candidates.append(next(value for value in match.groups() if value).strip())
    candidates += ["src/.env", ".env"]
    output: list[Path] = []
    for candidate in candidates:
        path = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
        if path.exists() and path not in output:
            output.append(path)
    return output


def load_local_runtime_env_snapshot(root: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    root = root or Path.cwd()
    files = resolve_local_runtime_env_files(root, env)
    values: dict[str, str] = {}
    for file in files:
        values.update(parse_env_file_content(file.read_text(encoding="utf-8")))
    return {"files": files, "values": values}


def apply_local_runtime_env(root: Path | None = None) -> dict[str, Any]:
    root = root or Path.cwd()
    snapshot = load_local_runtime_env_snapshot(root)
    os.environ.update(snapshot["values"])
    return snapshot

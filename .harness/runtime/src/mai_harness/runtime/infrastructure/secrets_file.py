"""Create and load the gitignored shell secret source for an environment."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute


def expected_secrets_source(env: str) -> str:
    return f".harness/secrets/{env}.sh"


def secrets_file_path(env: str) -> Path:
    if not isinstance(env, str) or not env:
        raise ValueError(f"env 必须为非空字符串，实际：{env}")
    return Path(expected_secrets_source(env))


def render_secrets_template(env: str, secret_names: list[str] | None = None) -> str:
    lines = [
        "#!/usr/bin/env bash",
        f"# Harness runtime secrets for {env}",
        "# Fill real values locally. This file is gitignored.",
        "",
    ]
    for name in dict.fromkeys(secret_names or []):
        lines.append(f"export {name}='{'22' if name.endswith('_DEPLOY_PORT') else ''}'")
    return "\n".join(lines) + "\n"


def ensure_secrets_file(env: str, secret_names: list[str] | None = None) -> dict[str, Any]:
    path = secrets_file_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    if created:
        path.write_text(render_secrets_template(env, secret_names), encoding="utf-8")
        path.chmod(0o600)
    return {"path": str(path), "created": created}


def load_secrets_file_snapshot(env: str, secret_names: list[str] | None = None) -> dict[str, Any]:
    path = secrets_file_path(env)
    if not path.exists():
        return {"path": str(path), "exists": False, "values": {}, "error": None}
    names = list(dict.fromkeys((secret_names or []) + ["HARNESS_SSH_KEY_PATH"]))
    script = 'set -a; source "$1"; shift; for name in "$@"; do value="${!name-}"; printf "%s=" "$name"; printf "%s" "$value" | base64 | tr -d "\\n"; printf "\\n"; done'
    try:
        outcome = execute(CommandSpec.argv_command(["bash", "-lc", script, "bash", str(path), *names]))
        if not outcome.ok:
            raise RuntimeError(outcome.stderr or outcome.stdout)
        output = outcome.stdout
        values = {}
        for line in output.splitlines():
            name, encoded = line.split("=", 1)
            value = base64.b64decode(encoded).decode() if encoded else ""
            if value:
                values[name] = value
        return {"path": str(path), "exists": True, "values": values, "error": None}
    except RuntimeError as exc:
        return {
            "path": str(path),
            "exists": True,
            "values": {},
            "error": str(exc).strip(),
        }


def apply_secrets_to_process_env(env: str, secret_names: list[str] | None = None) -> dict[str, Any]:
    snapshot = load_secrets_file_snapshot(env, secret_names)
    if snapshot["exists"] and not snapshot["error"]:
        os.environ.update(snapshot["values"])
    return snapshot

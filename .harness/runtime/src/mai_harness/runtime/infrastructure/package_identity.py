"""Canonical Python package and wheel identity helpers."""

from __future__ import annotations

import re


def wheel_version(filename: str, package: str) -> str:
    normalized = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", package))
    pattern = re.compile(
        rf"{normalized}-(?P<version>[^-]+)(?:-[^-]+)?-[^-]+-[^-]+-[^-]+\.whl",
        re.IGNORECASE,
    )
    match = pattern.fullmatch(filename)
    return match.group("version") if match else ""

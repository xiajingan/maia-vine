"""Canonical source/install path resolution for Harness runtime assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def resolve_project_relative(project: Path, configured: str, field: str) -> Path:
    """Resolve a configured project-owned path and reject boundary escapes."""
    relative = Path(configured)
    if relative.is_absolute():
        raise ValueError(f"{field}: 必须是工程内相对路径")
    root = project.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{field}: 不得越出工程目录")
    return target


def resolve_managed_project(control: Path, configured: str, field: str) -> Path:
    """Resolve a registered child or sibling Managed project."""
    relative = Path(configured)
    if relative.is_absolute() or configured.strip() in {"", "."}:
        raise ValueError(f"{field}: 必须是相对 Control 的 Managed 工程路径")
    root = control.resolve()
    target = (root / relative).resolve()
    is_child = target.is_relative_to(root) and target != root
    is_sibling = target.parent == root.parent and target != root
    if not (is_child or is_sibling):
        raise ValueError(f"{field}: Managed 必须位于 Control 内部或与 Control 平行")
    return target


@dataclass(frozen=True)
class HarnessPaths:
    project: Path
    runtime: Path

    @classmethod
    def detect(cls, anchor: Path | None = None, project: Path | None = None) -> HarnessPaths:
        location = (anchor or Path(__file__)).resolve()
        installed_runtime = next(
            (parent for parent in location.parents if parent.name == "runtime" and parent.parent.name == ".harness"),
            None,
        )
        if installed_runtime:
            project_root = (project or installed_runtime.parent.parent).resolve()
            return cls(project_root, installed_runtime)
        source_root = Path(__file__).resolve().parents[5]
        return cls((project or Path.cwd()).resolve(), source_root)

    @property
    def installed(self) -> bool:
        return self.runtime.parent.name == ".harness"

    @property
    def scripts(self) -> Path:
        return self.runtime / "src/mai_harness/runtime/commands"

    @property
    def config(self) -> Path:
        """Project-owned executable configuration (compatibility alias)."""
        return self.project_config

    @property
    def project_config(self) -> Path:
        return self.project / "config" if self.installed else self.runtime / "config"

    @property
    def framework_config(self) -> Path:
        return self.project / ".harness/framework" if self.installed else self.runtime / "config"

    @property
    def schemas(self) -> Path:
        return self.project / ".harness/schemas" if self.installed else self.runtime / "schemas"

    @property
    def state(self) -> Path:
        return self.project / ".harness/state"

    @property
    def legacy_framework_config(self) -> Path:
        return self.project / ".harness/config"

    @property
    def rules(self) -> Path:
        return self.project / ".harness/rules" if self.installed else self.runtime / "lint"

    @property
    def test_cases(self) -> Path:
        return self.project / "docs/test-cases" if self.installed else self.runtime / "test-cases"

    @property
    def e2e(self) -> Path:
        return self.project / "tests/e2e" if self.installed else self.runtime / "e2e"

    def script(self, name: str) -> Path:
        return self.scripts / name


PATHS = HarnessPaths.detect()

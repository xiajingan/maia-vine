"""Read-only source discovery and syntax extraction for data-lint."""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

from mai_harness.runtime.infrastructure.core.command import CommandSpec, execute
from mai_harness.runtime.infrastructure.core.paths import resolve_project_relative

EXTENSIONS = {".sql", ".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".json"}
IGNORED = {"node_modules", ".git", ".harness", ".venv", "__pycache__", "dist", "build"}


def source_files(root: Path, policy: dict) -> list[Path]:
    files: set[Path] = set()
    for configured in policy["sources"]:
        source = resolve_project_relative(root, configured, "data_policy.sources")
        candidates = [source] if source.is_file() else source.rglob("*") if source.is_dir() else []
        for path in candidates:
            if IGNORED.intersection(path.relative_to(root).parts):
                continue
            if not path.resolve().is_relative_to(root):
                raise ValueError(f"data_policy.sources: 符号链接越出工程 {path.relative_to(root)}")
            if path.suffix in EXTENSIONS and path.is_file():
                files.add(path)
    return sorted(files)


def python_facts(path: Path) -> list[dict]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    facts: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if method not in {"execute", "executemany", "query", "text", "sql"} or not node.args:
            continue
        argument = node.args[0]
        fact = {"file": str(path), "line": node.lineno}
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            facts.append({**fact, "kind": "sql", "text": argument.value})
        elif isinstance(argument, (ast.JoinedStr, ast.BinOp)):
            facts.append({**fact, "kind": "dynamic_sql"})
    return facts


def json_facts(path: Path) -> list[dict]:
    facts: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties", {})
            if isinstance(properties, dict):
                for name, schema in properties.items():
                    if not isinstance(schema, dict):
                        continue
                    types = schema.get("type", [])
                    if isinstance(types, str):
                        types = [types]
                    if isinstance(types, list) and any(t in {"number", "integer"} for t in types if isinstance(t, str)):
                        facts.append(
                            {
                                "file": str(path),
                                "line": 1,
                                "kind": "numeric_id",
                                "name": name,
                                "operation": "JSON Schema 数值类型",
                            }
                        )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(json.loads(path.read_text(encoding="utf-8")))
    return facts


def source_facts(root: Path, files: list[Path]) -> list[dict]:
    facts: list[dict] = []
    scripts: list[str] = []
    for path in files:
        if path.suffix == ".sql":
            facts.append({"file": str(path), "line": 1, "kind": "sql", "text": path.read_text(encoding="utf-8")})
        elif path.suffix == ".py":
            facts.extend(python_facts(path))
        elif path.suffix == ".json":
            facts.extend(json_facts(path))
        else:
            scripts.append(str(path))
    if scripts:
        helper = Path(__file__).with_name("data_source_facts.mjs")
        with tempfile.TemporaryDirectory(prefix="harness-data-lint-") as temp:
            request = Path(temp) / "request.json"
            request.write_text(json.dumps({"root": str(root), "files": scripts}), encoding="utf-8")
            result = execute(
                CommandSpec.argv_command(
                    ["node", str(helper), str(request)],
                    cwd=root,
                    timeout_seconds=60,
                    terminate_process_group=True,
                )
            )
        if not result.ok:
            raise ValueError(f"TS/JS AST 检查失败：{result.stderr.strip() or result.failure_kind}")
        facts.extend(json.loads(result.stdout))
    return facts

#!/usr/bin/env python3
import argparse
from pathlib import Path

from mai_harness.runtime.infrastructure.harness_config import load_harness_config
from mai_harness.runtime.infrastructure.utils import ok

ASSETS = {
    "dashboards": ("observability/dashboards", {".json"}, "JSON dashboard 导出"),
    "alerts": ("observability/alerts", {".yml", ".yaml", ".json"}, "项目 SLO 派生的告警规则"),
    "queries": ("observability/queries", {".promql", ".logql"}, "PromQL / LogQL 查询"),
    "runbooks": ("observability/runbooks", {".md"}, "On-call runbook"),
}


def required_assets() -> list[str]:
    return list(load_harness_config()["observability"]["required_assets"])


def validate(selected: list[str] | None = None) -> None:
    selected = required_assets() if selected is None else selected
    errors = [
        f"{directory}: 至少 1 个匹配文件（{hint}）"
        for name in selected
        for directory, suffixes, hint in [ASSETS[name]]
        if not any(
            path for path in Path(directory).glob("*") if not path.name.startswith(".") and path.suffix in suffixes
        )
    ]
    if errors:
        raise SystemExit("observability 校验失败：\n  - " + "\n  - ".join(errors))
    ok("observability 校验通过")


def scaffold(selected: list[str] | None = None) -> None:
    selected = required_assets() if selected is None else selected
    for name in selected:
        directory = ASSETS[name][0]
        Path(directory).mkdir(parents=True, exist_ok=True)
        marker = Path(directory, ".gitkeep")
        if not marker.exists():
            marker.write_text("# Add project-owned observability evidence here.\n", encoding="utf-8")
    ok("observability 骨架已生成")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "scaffold"))
    args = parser.parse_args()
    validate() if args.command == "validate" else scaffold()


if __name__ == "__main__":
    main()

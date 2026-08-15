#!/usr/bin/env python3
import argparse
import shutil
from pathlib import Path

from mai_harness.runtime.infrastructure.utils import ok

REQUIRED = {
    "observability/dashboards": "JSON dashboard 导出",
    "observability/alerts": "PromQL 告警规则",
    "observability/queries": "PromQL / LogQL 查询模板",
    "observability/runbooks": "On-call runbook",
}


def validate() -> None:
    errors = [
        f"{directory}: 至少 1 个文件（{hint}）"
        for directory, hint in REQUIRED.items()
        if not any(path for path in Path(directory).glob("*") if not path.name.startswith("."))
    ]
    if errors:
        raise SystemExit("observability 校验失败：\n  - " + "\n  - ".join(errors))
    ok("observability 校验通过")


def scaffold() -> None:
    for directory in REQUIRED:
        Path(directory).mkdir(parents=True, exist_ok=True)
    for kind in ("promql", "logql"):
        for source in Path("templates/observability", kind).glob("*"):
            target = Path("observability/queries", source.name)
            if not target.exists():
                shutil.copy2(source, target)
    defaults = {
        "observability/alerts/error-rate.yml": "expr: error_rate_5xx > 0.01\nfor: 5m\nseverity: warning\n",
        "observability/runbooks/error-rate.md": "# Error rate runbook\n\n1. 检查日志和指标\n2. 执行 `uv run --project .harness/runtime harness deploy rollback --env <env>`\n",
        "observability/dashboards/.gitkeep": "# Export dashboards here\n",
    }
    for name, content in defaults.items():
        Path(name).write_text(content, encoding="utf-8") if not Path(name).exists() else None
    ok("observability 骨架已生成")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "scaffold"))
    args = parser.parse_args()
    validate() if args.command == "validate" else scaffold()


if __name__ == "__main__":
    main()

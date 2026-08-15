#!/usr/bin/env python3
"""Run declarative prototype-versus-live UI fidelity checks with Playwright."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.paths import PATHS
from mai_harness.runtime.infrastructure.ui_contracts import load_contracts, validate_contracts

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def locator_values(page: Any, selector: str, mode: str, property_name: str = "") -> Any:
    locator = page.locator(selector)
    if mode == "count":
        return locator.count()
    if not locator.count():
        return "ELEMENT_MISSING" if mode != "texts" else []
    if mode == "texts":
        return [normalize_text(item) for item in locator.all_text_contents() if normalize_text(item)]
    if mode == "style":
        return locator.first.evaluate("(node, prop) => getComputedStyle(node)[prop]", property_name)
    if mode == "metric":
        return locator.first.evaluate(
            """(node, metric) => { const r=node.getBoundingClientRect(); if(Number.isFinite(r[metric])) return r[metric]; const v=getComputedStyle(node)[metric]; return v && v.endsWith('px') ? parseFloat(v) : v; }""",
            property_name,
        )
    return None


def run_check(prototype_page: Any, live_page: Any, check: dict[str, Any]) -> dict[str, Any]:
    kind = check["kind"]
    label = check.get("label", kind)
    prototype_selector = check.get("prototype_selector", check.get("selector", ""))
    live_selector = check.get("live_selector", check.get("selector", ""))
    if kind in {"presence", "count"}:
        left = locator_values(prototype_page, prototype_selector, "count")
        right = locator_values(live_page, live_selector, "count")
        passed = left > 0 and right > 0 if kind == "presence" else left > 0 and left == right
    elif kind == "textList":
        left = locator_values(prototype_page, prototype_selector, "texts")
        right = locator_values(live_page, live_selector, "texts")
        passed = bool(left) and left == right
    elif kind == "style":
        prop = check.get("property", "")
        left = locator_values(prototype_page, prototype_selector, "style", prop)
        right = locator_values(live_page, live_selector, "style", prop)
        passed = left != "ELEMENT_MISSING" and left == right
    elif kind == "metric":
        metric = check.get("metric", "")
        left = locator_values(prototype_page, prototype_selector, "metric", metric)
        right = locator_values(live_page, live_selector, "metric", metric)
        try:
            passed = abs(float(left) - float(right)) <= float(check.get("tolerance_px", 12))
        except (TypeError, ValueError):
            passed = False
    else:
        return {
            "label": label,
            "prototypeActual": "UNSUPPORTED",
            "liveActual": "UNSUPPORTED",
            "passed": False,
            "error": f"未知 check.kind: {kind}",
        }
    return {"label": label, "prototypeActual": left, "liveActual": right, "passed": passed}


def apply_actions(page: Any, actions: list[dict[str, Any]]) -> None:
    for action in actions:
        kind = action.get("action")
        selector = action.get("selector", "")
        if kind == "click":
            page.locator(selector).click()
        elif kind == "fill":
            page.locator(selector).fill(str(action.get("value", "")))
        elif kind == "press":
            page.locator(selector).press(str(action.get("key", "Enter")))
        elif kind == "wait":
            page.wait_for_timeout(int(action.get("milliseconds", 800)))
        else:
            raise ValueError(f"不支持的 prepare action: {kind}")


def audit(sprint: str, plan: dict[str, Any], root: Path, web_base: str, screenshot_root: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "sprintId": sprint,
        "collectedAt": datetime.now(UTC).isoformat(),
        "environment": {"webBase": web_base},
        "required": plan["required"],
        "reason": plan.get("reason", ""),
        "pages": [],
    }
    if not plan["required"]:
        report.update({"mode": "not-required", "passed": True})
        return report
    errors = validate_contracts(plan)
    if errors:
        raise ValueError("UI contract schema 失败: " + "; ".join(errors))
    if not plan["contracts"]:
        raise ValueError(f"Sprint {sprint} 未声明 UI contract")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Python Playwright；运行 uv sync，并执行 playwright install chromium") from exc
    screenshot_dir = screenshot_root / sprint
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for contract in plan["contracts"]:
                viewport = contract.get("viewport", DEFAULT_VIEWPORT)
                prototype = browser.new_page(viewport=viewport)
                live = browser.new_page(viewport=viewport)
                prototype_target, live_target = contract["prototype"], contract["live"]
                prototype.goto((root / prototype_target["path"]).resolve().as_uri(), wait_until="domcontentloaded")
                live.goto(web_base.rstrip("/") + live_target["path"], wait_until="networkidle")
                for page, target in ((prototype, prototype_target), (live, live_target)):
                    if target.get("ready_selector"):
                        page.locator(target["ready_selector"]).first.wait_for(state="visible", timeout=10_000)
                    apply_actions(page, target.get("prepare", []))
                    page.wait_for_timeout(1200)
                checks = [run_check(prototype, live, item) for item in contract["checks"]]
                name = re.sub(r"[^A-Za-z0-9_-]", "-", contract.get("screenshot_name", contract["name"]))
                prototype_shot, live_shot = (
                    screenshot_dir / f"{name}-prototype.png",
                    screenshot_dir / f"{name}-live.png",
                )
                prototype.screenshot(path=str(prototype_shot), full_page=True)
                live.screenshot(path=str(live_shot), full_page=True)
                report["pages"].append(
                    {
                        "name": contract["name"],
                        "designRef": contract.get("design_ref", ""),
                        "prototypePath": prototype_target["path"],
                        "livePath": live_target["path"],
                        "viewport": viewport,
                        "prototypeScreenshotPath": str(prototype_shot),
                        "liveScreenshotPath": str(live_shot),
                        "checks": checks,
                        "passed": all(item["passed"] for item in checks),
                    }
                )
                prototype.close()
                live.close()
        finally:
            browser.close()
    report.update(
        {
            "mode": "prototype-parity",
            "passed": bool(report["pages"]) and all(page["passed"] for page in report["pages"]),
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sprint", required=True)
    parser.add_argument("--contracts", type=Path, default=PATHS.rules / "ui-contracts.yml")
    parser.add_argument("--report-path", type=Path, default=Path("coverage/ui-audit.json"))
    parser.add_argument("--web-url", default=os.environ.get("WEB_URL", "http://localhost:5173"))
    parser.add_argument("--screenshot-dir", type=Path, default=Path("coverage/ui-audit"))
    args = parser.parse_args()
    try:
        report = audit(
            args.sprint, load_contracts(args.contracts, args.sprint), Path.cwd(), args.web_url, args.screenshot_dir
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{'✅' if report['passed'] else '❌'} UI 审核{'通过' if report['passed'] else '未通过'}: {args.report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

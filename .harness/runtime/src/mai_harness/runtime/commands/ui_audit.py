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

# Calibrated to the observed 255/255/255 versus 249/250/251 false negative.
BACKGROUND_RGB_CHANNEL_TOLERANCE = 6
RGB_COLOR_PATTERN = re.compile(r"^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$")

EFFECTIVE_BACKGROUND_EVALUATOR = r"""
(node) => {
  const parseColor = (value) => {
    const legacy = value.match(
      /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/
    );
    if (legacy) {
      return [
        Number(legacy[1]),
        Number(legacy[2]),
        Number(legacy[3]),
        legacy[4] === undefined ? 1 : Number(legacy[4]),
      ];
    }
    const canvas = document.createElement('canvas');
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const pixel = context.getImageData(0, 0, 1, 1).data;
    return [pixel[0], pixel[1], pixel[2], pixel[3] / 255];
  };
  const over = (foreground, background) => {
    const alpha = foreground[3] + background[3] * (1 - foreground[3]);
    if (alpha === 0) return [0, 0, 0, 0];
    const channel = (index) => (
      foreground[index] * foreground[3]
      + background[index] * background[3] * (1 - foreground[3])
    ) / alpha;
    return [channel(0), channel(1), channel(2), alpha];
  };
  const layers = [];
  for (let current = node; current; current = current.parentElement) {
    layers.push(parseColor(getComputedStyle(current).backgroundColor));
  }
  let result = [255, 255, 255, 1];
  for (let index = layers.length - 1; index >= 0; index -= 1) {
    result = over(layers[index], result);
  }
  const channels = result.slice(0, 3).map((channel) => Math.round(channel));
  if (result[3] >= 1) return `rgb(${channels.join(', ')})`;
  return `rgba(${channels.join(', ')}, ${Number(result[3].toFixed(3))})`;
}
"""


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_rgb_channels(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, str) or not (match := RGB_COLOR_PATTERN.fullmatch(value)):
        return None
    try:
        channels = tuple(float(match.group(index)) for index in range(1, 4))
        alpha = 1.0 if match.group(4) is None else float(match.group(4))
    except ValueError:
        return None
    if any(channel < 0 or channel > 255 for channel in channels) or not 0 <= alpha <= 1:
        return None
    return channels


def background_colors_match(left: Any, right: Any) -> bool:
    left_channels = parse_rgb_channels(left)
    right_channels = parse_rgb_channels(right)
    if left_channels is None or right_channels is None:
        return left == right
    return all(
        abs(left_channel - right_channel) <= BACKGROUND_RGB_CHANNEL_TOLERANCE
        for left_channel, right_channel in zip(left_channels, right_channels, strict=True)
    )


def locator_values(page: Any, selector: str, mode: str, property_name: str = "") -> Any:
    locator = page.locator(selector)
    if mode == "count":
        return locator.count()
    if not locator.count():
        return [] if mode in {"texts", "direct_texts"} else "ELEMENT_MISSING"
    if mode == "texts":
        return [normalize_text(item) for item in locator.all_text_contents() if normalize_text(item)]
    if mode == "direct_texts":
        values = locator.evaluate_all(
            """nodes => nodes.map(node => Array.from(node.childNodes)
              .filter(child => child.nodeType === Node.TEXT_NODE)
              .map(child => child.textContent || '').join(' '))"""
        )
        return [normalize_text(item) for item in values if normalize_text(item)]
    if mode == "style":
        if property_name.replace("-", "").lower() == "backgroundcolor":
            return locator.first.evaluate(EFFECTIVE_BACKGROUND_EVALUATOR)
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
        text_mode = "direct_texts" if check.get("text_mode") == "direct" else "texts"
        left = locator_values(prototype_page, prototype_selector, text_mode)
        right = locator_values(live_page, live_selector, text_mode)
        passed = bool(left) and left == right
    elif kind == "style":
        prop = check.get("property", "")
        left = locator_values(prototype_page, prototype_selector, "style", prop)
        right = locator_values(live_page, live_selector, "style", prop)
        if "ELEMENT_MISSING" in {left, right}:
            passed = False
        elif prop.replace("-", "").lower() == "backgroundcolor":
            passed = background_colors_match(left, right)
        else:
            passed = left == right
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


def apply_actions(page: Any, actions: list[dict[str, Any]], environment: dict[str, str]) -> None:
    for action in actions:
        kind = action.get("action")
        selector = action.get("selector", "")
        if kind == "click":
            page.locator(selector).click()
        elif kind == "fill":
            value = action.get("value")
            value_env = action.get("value_env")
            if value_env is not None:
                value = environment.get(value_env)
                if not value:
                    raise ValueError(f"prepare fill 环境变量缺失: {value_env}")
            page.locator(selector).fill(str(value or ""))
        elif kind == "press":
            page.locator(selector).press(str(action.get("key", "Enter")))
        elif kind == "wait":
            page.wait_for_timeout(int(action.get("milliseconds", 800)))
        else:
            raise ValueError(f"不支持的 prepare action: {kind}")


def audit(
    sprint: str,
    plan: dict[str, Any],
    root: Path,
    web_base: str,
    screenshot_root: Path,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "sprintId": sprint,
        "collectedAt": datetime.now(UTC).isoformat(),
        "environment": {"webBase": web_base},
        "required": plan["required"],
        "reason": plan.get("reason", ""),
        "pages": [],
        "runId": (environment or {}).get("HARNESS_QUALITY_RUN_ID"),
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
                    apply_actions(page, target.get("prepare", []), environment or {})
                    if target.get("ready_selector"):
                        page.locator(target["ready_selector"]).first.wait_for(state="visible", timeout=10_000)
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
            args.sprint,
            load_contracts(args.contracts, args.sprint),
            Path.cwd(),
            args.web_url,
            args.screenshot_dir,
            dict(os.environ),
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

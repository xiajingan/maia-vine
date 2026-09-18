"""Verify host-owned human interaction events; the Harness CLI never signs a decision."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

INTERACTION_PROOF_KEY_ENV = "HARNESS_INTERACTION_PROOF_KEY"


def interaction_proof_digest(signoff: dict[str, Any], key: str) -> str:
    payload = {name: value for name, value in signoff.items() if name != "interaction_proof"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()


def verify_interaction(event: Any, request_sha256: str, created_at: str) -> dict[str, Any]:
    """Check an actual host decision for the displayed request, not an agent's attribution."""
    required = {
        "schema_version",
        "source",
        "interaction_id",
        "actor",
        "decision",
        "request_sha256",
        "occurred_at",
        "interaction_proof",
    }
    if not isinstance(event, dict) or set(event) != required or event.get("schema_version") != 1:
        raise ValueError("人工决定必须是宿主交互事件 v1，包含精确请求摘要与交互证明")
    if event["source"] != "host-user-interaction" or event["decision"] not in {"approved", "rejected"}:
        raise ValueError("必须由宿主记录用户的 approved/rejected 决定；Agent 不能自签")
    if event["request_sha256"] != request_sha256:
        raise ValueError("人工决定不属于当前产物版本或 US/AC 选择")
    if not all(isinstance(event[key], str) and event[key].strip() for key in required - {"schema_version"}):
        raise ValueError("人工交互事件字段必须是非空字符串")
    key = os.environ.get(INTERACTION_PROOF_KEY_ENV, "")
    if len(key) < 32:
        raise ValueError("宿主尚未接入可信人工交互证明，保持等待；不得由 Agent 生成密钥或批准")
    if not hmac.compare_digest(event["interaction_proof"], interaction_proof_digest(event, key)):
        raise ValueError("人工交互证明无效；审批人姓名或 source 字段不能替代真实用户事件")
    try:
        occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if occurred.tzinfo is None or created.tzinfo is None or not created <= occurred <= datetime.now(UTC):
            raise ValueError("人工决定必须发生在请求形成之后，且时间必须含时区")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"人工交互事件时间无效: {exc}") from exc
    return event

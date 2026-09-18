"""Version-bound requests and durable human decisions shared by Pick and design tasks."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.codex_interaction import native_context, native_decision
from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.interaction import INTERACTION_PROOF_KEY_ENV, verify_interaction


def payload_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def request_approval(root: Path, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the existing request for unchanged content; retries do not ask again."""
    identity = {"schema_version": 1, "kind": kind, "payload": payload}
    identifier = payload_digest(identity)
    store = StateStore(root / ".harness/state/approvals/requests")
    with store.lock(identifier):
        request = store.read_json(f"{identifier}.json", None)
        if request is None:
            request = {**identity, "created_at": datetime.now(UTC).isoformat()}
            if not os.environ.get(INTERACTION_PROOF_KEY_ENV):
                request["host_context"] = native_context(root)
            request["request_sha256"] = payload_digest(request)
            store.write_json(f"{identifier}.json", request)
        if {key: request.get(key) for key in identity} != identity:
            raise ValueError("人工审核请求身份冲突")
    return {
        "request": store.path(f"{identifier}.json").relative_to(root).as_posix(),
        "request_sha256": request["request_sha256"],
        "kind": kind,
        "payload": payload,
        "reply": f"批准 {request['request_sha256']} / 拒绝 {request['request_sha256']}"
        if request.get("host_context")
        else None,
    }


def read_request(root: Path, reference: dict[str, Any]) -> dict[str, Any]:
    relative = Path(str(reference.get("request", "")))
    directory = root / ".harness/state/approvals/requests"
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("人工审核请求必须是 Runtime 登记的工程内文件")
    request = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(request, dict)
        or request.get("request_sha256")
        != payload_digest({key: value for key, value in request.items() if key != "request_sha256"})
        or request.get("request_sha256") != reference.get("request_sha256")
    ):
        raise ValueError("人工审核请求缺失或内容已变化")
    if request.get("payload") != reference.get("payload") or request.get("kind") != reference.get("kind"):
        raise ValueError("人工审核请求不属于当前内容")
    return request


def record_decision(root: Path, reference: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    request = read_request(root, reference)
    verify_decision(root, request, event)
    store = StateStore(root / ".harness/state/approvals/decisions")
    name = request["request_sha256"] + ".json"
    with store.lock(name):
        existing = store.read_json(name, None)
        if existing is not None and existing != event:
            raise ValueError("当前请求已有不同人工决定；请修订后提交新请求")
        store.write_json(name, event)
    return event


def require_approval(root: Path, reference: dict[str, Any]) -> dict[str, Any]:
    request = read_request(root, reference)
    event = StateStore(root / ".harness/state/approvals/decisions").read_json(request["request_sha256"] + ".json", None)
    if event is None:
        raise ValueError("等待人工确认当前版本；Agent Review PASS 不能替代人工批准")
    verify_decision(root, request, event)
    if event["decision"] != "approved":
        raise ValueError("人工已拒绝当前版本，必须修订并重新 Review")
    return event


def verify_decision(root: Path, request: dict[str, Any], event: dict[str, Any]) -> None:
    if event.get("source") == "codex-user-message":
        if event != native_decision(root, request):
            raise ValueError("人工决定与宿主会话原记录不一致")
    else:
        verify_interaction(event, request["request_sha256"], request["created_at"])


def read_native_decision(root: Path, reference: dict[str, Any]) -> dict[str, Any]:
    return native_decision(root, read_request(root, reference))

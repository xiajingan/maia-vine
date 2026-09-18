"""Read native Codex user replies without generating a signature or editing host history.

The host's persisted history is the trust boundary, like a host's signing key.
This adapter is not an attestation against a process able to rewrite host state.
Unsupported/missing session records fail closed; no caller-supplied transcript is accepted.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _home(root: Path) -> Path:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
    if home.is_relative_to(root.resolve()):
        raise ValueError("Codex 交互记录必须由工程外的宿主管理，不能使用工程内伪造记录")
    return home


def _session_log(home: Path, session_id: str) -> Path:
    if not session_id or any(ch not in "0123456789abcdef-" for ch in session_id):
        raise ValueError("Codex thread ID 不合法")
    matches = [
        path for folder in ("sessions", "archived_sessions") for path in (home / folder).rglob(f"*-{session_id}.jsonl")
    ]
    if len(matches) != 1 or matches[0].is_symlink() or not matches[0].resolve().is_relative_to(home):
        raise ValueError("未找到唯一的 Codex 宿主会话记录；保持等待")
    return matches[0]


def native_context(root: Path) -> dict[str, Any] | None:
    session = os.environ.get("CODEX_THREAD_ID")
    if not session:
        return None
    home = _home(root)
    raw = _session_log(home, session).read_bytes()
    # Bind only complete host records; the host may currently be appending a line.
    prefix = raw[: raw.rfind(b"\n") + 1]
    first = json.loads(prefix.splitlines()[0]) if prefix else {}
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != session:
        raise ValueError("不支持的 Codex 会话格式；保持等待")
    return {
        "provider": "codex",
        "session_id": session,
        "home": str(home),
        "prefix_bytes": len(prefix),
        "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
    }


def native_decision(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    context = request.get("host_context") or {}
    home = _home(root)
    if context.get("provider") != "codex" or context.get("home") != str(home):
        raise ValueError("当前请求没有绑定本机 Codex 会话；使用已配置宿主事件入口")
    raw = _session_log(home, context["session_id"]).read_bytes()
    size = context["prefix_bytes"]
    if hashlib.sha256(raw[:size]).hexdigest() != context["prefix_sha256"]:
        raise ValueError("请求之前的宿主会话记录已变化，不能采信人工决定")
    created = datetime.fromisoformat(request["created_at"])
    digest = request["request_sha256"]
    decisions = {f"批准 {digest}": "approved", f"拒绝 {digest}": "rejected"}
    for number, line in enumerate(raw[size:].splitlines(), start=1):
        try:
            record = json.loads(line)
        except ValueError:
            continue  # A partially flushed final record is not a user decision.
        payload = record.get("payload") or {}
        if record.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
            continue
        content = payload.get("content") or []
        if not content or any(item.get("type") != "input_text" for item in content):
            continue
        text = "\n".join(item.get("text", "") for item in content).strip()
        if text not in decisions:
            continue
        occurred = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        if occurred.tzinfo is None or not created <= occurred <= datetime.now(UTC):
            continue
        return {
            "schema_version": 1,
            "source": "codex-user-message",
            "interaction_id": f"{context['session_id']}:{size}:{number}",
            "actor": "Codex user",
            "decision": decisions[text],
            "request_sha256": digest,
            "occurred_at": record["timestamp"],
            "record_sha256": hashlib.sha256(line).hexdigest(),
        }
    raise ValueError(f"尚未收到绑定当前请求的用户回复；请在原 Codex 会话回复：批准 {digest}（或：拒绝 {digest}）")

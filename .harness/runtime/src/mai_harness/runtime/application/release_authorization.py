"""Challenge, issue, validate, and consume explicit human rollback authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mai_harness.runtime.infrastructure.core.state_store import StateStore
from mai_harness.runtime.infrastructure.utils import load_yaml, write_yaml

REQUIRED_FIELDS = {
    "action",
    "authorization_hmac",
    "authorization_id",
    "candidate_ref",
    "challenge_sha256",
    "decision",
    "interaction_id",
    "interaction_proof",
    "interaction_proof_sha256",
    "issuer",
    "nonce",
    "reason",
    "receipt_sha256",
    "requested_at",
    "expires_at",
    "requested_by",
    "signoff_sha256",
    "source",
    "target_ref",
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
INTERACTION_PROOF_KEY_ENV = "HARNESS_INTERACTION_PROOF_KEY"
MAX_AUTHORIZATION_WINDOW = timedelta(minutes=15)


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def authorization_digest(record: dict[str, Any]) -> str:
    return _digest({key: value for key, value in record.items() if key != "receipt_sha256"})


def authorization_hmac_digest(record: dict[str, Any], key: str) -> str:
    """Authenticate every authorization identity and decision field."""
    payload = {name: value for name, value in record.items() if name not in {"authorization_hmac", "receipt_sha256"}}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()


def interaction_proof_digest(signoff: dict[str, Any], key: str) -> str:
    """Digest an externally signed interaction event; Harness only verifies it."""
    payload = {name: value for name, value in signoff.items() if name != "interaction_proof"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), encoded, hashlib.sha256).hexdigest()


def _aware_timestamp(value: Any, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"发布回滚 {field} 必须是 ISO8601") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"发布回滚 {field} 必须含时区")
    return timestamp


def create_rollback_challenge(
    state_root: Path, authorization_id: str, target_ref: str, candidate_ref: str, interaction_id: str
) -> tuple[Path, dict[str, Any]]:
    for field, value in (("authorization_id", authorization_id), ("interaction_id", interaction_id)):
        if not IDENTIFIER.fullmatch(value):
            raise ValueError(f"发布回滚 challenge.{field} 必须是稳定 ID")
    if not target_ref or not candidate_ref:
        raise ValueError("发布回滚 challenge 必须绑定 target_ref/candidate_ref")
    store = StateStore(state_root / "rollback-authorizations/challenges")
    candidate = {
        "authorization_id": authorization_id,
        "target_ref": target_ref,
        "candidate_ref": candidate_ref,
        "interaction_id": interaction_id,
        "nonce": secrets.token_hex(32),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    candidate["challenge_sha256"] = _digest(candidate)

    def create(current: Any) -> dict[str, Any]:
        if current:
            fields = ("authorization_id", "target_ref", "candidate_ref", "interaction_id")
            if {key: current.get(key) for key in fields} != {key: candidate[key] for key in fields}:
                raise ValueError("发布回滚 challenge ID 已绑定其他目标")
            return current
        return candidate

    challenge = store.update_json(f"{authorization_id}.json", create, default={})
    return store.path(f"{authorization_id}.json"), challenge


def require_release_rollback_authorization(
    path: Path | None,
    expected_target: str,
    expected_candidate: str,
    *,
    record_override: dict[str, Any] | None = None,
    proof_key: str | None = None,
) -> dict[str, Any]:
    if not expected_target:
        raise ValueError("没有可绑定的当前 Release/tag，禁止发布回滚")
    if path is None and record_override is None:
        raise ValueError("发布回滚必须提供人员明确确认的 --authorization 文件")
    if path is not None and not path.is_file():
        raise ValueError(f"发布回滚 authorization 不存在: {path}")
    record = record_override if record_override is not None else load_yaml(path)
    if not isinstance(record, dict) or set(record) != REQUIRED_FIELDS:
        raise ValueError(f"发布回滚 authorization 必须且只能声明 {sorted(REQUIRED_FIELDS)}")
    if record.get("decision") != "approved" or record.get("action") != "release-rollback":
        raise ValueError("发布回滚 authorization 必须明确批准 release-rollback")
    if record.get("source") != "ask_user":
        raise ValueError("发布回滚 authorization 必须来自当前主交互的 ask_user 明确确认")
    if record.get("issuer") != "mai-harness:release-authorization":
        raise ValueError("发布回滚 authorization 必须由 Harness challenge/signoff 流程签发")
    if not isinstance(record.get("requested_by"), str) or not record["requested_by"].strip():
        raise ValueError("发布回滚 authorization.requested_by 必填")
    if not isinstance(record.get("reason"), str) or not record["reason"].strip():
        raise ValueError("发布回滚 authorization.reason 必填")
    for field in ("authorization_id", "interaction_id"):
        if not isinstance(record.get(field), str) or not IDENTIFIER.fullmatch(record[field]):
            raise ValueError(f"发布回滚 authorization.{field} 必须是稳定 ID")
    if not re.fullmatch(r"[0-9a-f]{32,128}", str(record.get("nonce", ""))):
        raise ValueError("发布回滚 authorization.nonce 必须是随机十六进制 challenge")
    if record.get("target_ref") != expected_target:
        raise ValueError(f"发布回滚 authorization.target_ref 必须绑定 {expected_target}")
    if not expected_candidate or record.get("candidate_ref") != expected_candidate:
        raise ValueError(f"发布回滚 authorization.candidate_ref 必须绑定 {expected_candidate}")
    if record.get("receipt_sha256") != authorization_digest(record):
        raise ValueError("发布回滚 authorization.receipt_sha256 与授权内容不一致")
    _aware_timestamp(record.get("requested_at"), "authorization.requested_at")
    expires_at = _aware_timestamp(record.get("expires_at"), "authorization.expires_at")
    if datetime.now().astimezone() > expires_at:
        raise ValueError("发布回滚 authorization 已过期")
    trusted_key = proof_key if proof_key is not None else os.environ.get(INTERACTION_PROOF_KEY_ENV, "")
    if len(trusted_key) < 32:
        raise ValueError(f"发布回滚验证缺少运行时可信交互证明密钥 {INTERACTION_PROOF_KEY_ENV}")
    signoff_payload = {
        key: record[key]
        for key in (
            "decision",
            "source",
            "requested_by",
            "requested_at",
            "expires_at",
            "reason",
            "challenge_sha256",
            "interaction_id",
            "interaction_proof",
        )
    }
    if not hmac.compare_digest(
        str(record.get("interaction_proof", "")), interaction_proof_digest(signoff_payload, trusted_key)
    ):
        raise ValueError("发布回滚 authorization 交互证明验签失败")
    if not hmac.compare_digest(
        str(record.get("authorization_hmac", "")), authorization_hmac_digest(record, trusted_key)
    ):
        raise ValueError("发布回滚 authorization 完整身份验签失败")
    return record


def issue_release_rollback_authorization(
    root: Path,
    state_root: Path,
    authorization_id: str,
    signoff_path: Path,
    *,
    proof_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    challenge_store = StateStore(state_root / "rollback-authorizations/challenges")
    challenge = challenge_store.read_json(f"{authorization_id}.json", {})
    if not challenge or challenge.get("challenge_sha256") != _digest(
        {key: value for key, value in challenge.items() if key != "challenge_sha256"}
    ):
        raise ValueError("发布回滚 challenge 不存在或摘要无效")
    resolved_signoff = signoff_path.resolve()
    if not resolved_signoff.is_file() or not resolved_signoff.is_relative_to(root.resolve()):
        raise ValueError("发布回滚 signoff 必须是工程内文件")
    signoff = load_yaml(resolved_signoff)
    required_signoff = {
        "decision",
        "source",
        "requested_by",
        "requested_at",
        "expires_at",
        "reason",
        "challenge_sha256",
        "interaction_id",
        "interaction_proof",
    }
    if not isinstance(signoff, dict) or set(signoff) != required_signoff:
        raise ValueError(f"发布回滚 signoff 必须且只能声明 {sorted(required_signoff)}")
    if (
        signoff.get("decision") != "approved"
        or signoff.get("source") != "ask_user"
        or signoff.get("challenge_sha256") != challenge["challenge_sha256"]
        or signoff.get("interaction_id") != challenge["interaction_id"]
    ):
        raise ValueError("发布回滚 signoff 未批准当前 challenge")
    trusted_key = proof_key if proof_key is not None else os.environ.get(INTERACTION_PROOF_KEY_ENV, "")
    if len(trusted_key) < 32:
        raise ValueError(f"发布回滚签发缺少运行时可信交互证明密钥 {INTERACTION_PROOF_KEY_ENV}")
    supplied_proof = str(signoff.get("interaction_proof", ""))
    expected_proof = interaction_proof_digest(signoff, trusted_key)
    if not hmac.compare_digest(supplied_proof, expected_proof):
        raise ValueError("发布回滚 signoff 缺少有效的运行时交互证明")
    created_at = _aware_timestamp(challenge.get("created_at"), "challenge.created_at")
    requested_at = _aware_timestamp(signoff.get("requested_at"), "signoff.requested_at")
    expires_at = _aware_timestamp(signoff.get("expires_at"), "signoff.expires_at")
    current = now or datetime.now().astimezone()
    if requested_at < created_at or expires_at <= requested_at:
        raise ValueError("发布回滚 signoff 必须在 challenge 创建后签署并声明未来过期时间")
    if expires_at - requested_at > MAX_AUTHORIZATION_WINDOW or current > expires_at:
        raise ValueError("发布回滚 signoff 已过期或有效窗口超过 15 分钟")
    if requested_at - current > timedelta(minutes=1):
        raise ValueError("发布回滚 signoff.requested_at 不得晚于当前时间")
    record = {
        "decision": "approved",
        "action": "release-rollback",
        "authorization_id": authorization_id,
        "candidate_ref": challenge["candidate_ref"],
        "challenge_sha256": challenge["challenge_sha256"],
        "source": "ask_user",
        "interaction_id": challenge["interaction_id"],
        "interaction_proof": supplied_proof,
        "interaction_proof_sha256": hashlib.sha256(supplied_proof.encode()).hexdigest(),
        "issuer": "mai-harness:release-authorization",
        "nonce": challenge["nonce"],
        "requested_by": signoff.get("requested_by"),
        "requested_at": signoff.get("requested_at"),
        "expires_at": signoff.get("expires_at"),
        "reason": signoff.get("reason"),
        "signoff_sha256": hashlib.sha256(resolved_signoff.read_bytes()).hexdigest(),
        "target_ref": challenge["target_ref"],
    }
    record["authorization_hmac"] = authorization_hmac_digest(record, trusted_key)
    record["receipt_sha256"] = authorization_digest(record)
    require_release_rollback_authorization(
        None,
        challenge["target_ref"],
        challenge["candidate_ref"],
        record_override=record,
        proof_key=trusted_key,
    )
    issued = state_root / "rollback-authorizations/issued" / f"{authorization_id}.yml"
    with StateStore(state_root / "rollback-authorizations").lock(f"issue.{authorization_id}"):
        if issued.exists() and load_yaml(issued) != record:
            raise ValueError("发布回滚 authorization 已签发且内容不同")
        write_yaml(issued, record)
    return issued, record


def consume_release_rollback_authorization(
    path: Path | None,
    expected_target: str,
    expected_candidate: str,
    state_root: Path,
    consumer_id: str,
    *,
    proof_key: str | None = None,
) -> dict[str, Any]:
    """Bind one issued authorization to one operation, allowing same-consumer retries."""
    record = require_release_rollback_authorization(path, expected_target, expected_candidate, proof_key=proof_key)
    if not IDENTIFIER.fullmatch(consumer_id):
        raise ValueError("发布回滚 authorization consumer_id 必须是稳定 ID")
    issued = state_root / "rollback-authorizations/issued" / f"{record['authorization_id']}.yml"
    if path is None or path.resolve() != issued.resolve():
        raise ValueError("发布回滚必须使用 Harness 签发目录中的 authorization receipt")
    challenge = StateStore(state_root / "rollback-authorizations/challenges").read_json(
        f"{record['authorization_id']}.json", {}
    )
    challenge_digest = (
        _digest({key: value for key, value in challenge.items() if key != "challenge_sha256"})
        if isinstance(challenge, dict) and challenge
        else ""
    )
    if (
        not challenge
        or challenge_digest != challenge.get("challenge_sha256")
        or challenge.get("challenge_sha256") != record.get("challenge_sha256")
        or any(
            challenge.get(field) != record.get(field)
            for field in ("authorization_id", "interaction_id", "target_ref", "candidate_ref", "nonce")
        )
    ):
        raise ValueError("发布回滚 authorization 缺少匹配的原始 challenge")
    receipt_digest = record["receipt_sha256"]
    store = StateStore(state_root / "rollback-authorizations/consumed")

    def consume(current: Any) -> dict[str, Any]:
        if current:
            if current.get("receipt_sha256") == receipt_digest and current.get("consumer_id") == consumer_id:
                return current
            raise ValueError("发布回滚 authorization 已被其他操作消费")
        return {
            "authorization_id": record["authorization_id"],
            "receipt_sha256": receipt_digest,
            "target_ref": expected_target,
            "candidate_ref": expected_candidate,
            "consumer_id": consumer_id,
            "consumed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    store.update_json(f"{record['authorization_id']}.json", consume, default={})
    return record

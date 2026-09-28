from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from terminal_mcp.core.orchestration import parse_utc, public_agent_name


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class AgentIdentityRecord:
    source_instance_id: str
    agent_id: str
    state: str
    session_started_at: str
    expires_at: str
    updated_at: str
    revision: int
    ended_at: str | None = None
    end_reason: str | None = None
    payload_version: int = 1
    task_summary: str = ""
    intent: str = ""
    work_scope: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    current_step: int = 1

    def __post_init__(self):
        if not self.source_instance_id.strip():
            raise ValueError("source_instance_id must not be empty")
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.state not in {"active", "finished", "forced"}:
            raise ValueError("state must be active, finished or forced")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.payload_version not in {1, 2}:
            raise ValueError("payload_version must be 1 or 2")
        if self.state == "active" and self.ended_at is not None:
            raise ValueError("active identity must not have ended_at")
        if self.state == "active" and self.end_reason is not None:
            raise ValueError("active identity must not have end_reason")
        if self.state != "active" and self.ended_at is None:
            raise ValueError("terminal identity requires ended_at")
        if self.state != "active" and not self.end_reason:
            raise ValueError("terminal identity requires end_reason")
        started = parse_utc(self.session_started_at)
        expires = parse_utc(self.expires_at)
        parse_utc(self.updated_at)
        if expires <= started:
            raise ValueError("expires_at must be later than session_started_at")
        if self.ended_at is not None:
            parse_utc(self.ended_at)
        if self.payload_version >= 2:
            if not self.task_summary:
                raise ValueError("v2 identity requires task_summary")
            if not self.intent:
                raise ValueError("v2 identity requires intent")
            if not self.details:
                raise ValueError("v2 identity requires details")
            if self.current_step < 1 or self.current_step > len(self.details):
                raise ValueError("v2 identity current_step is outside details")

    @property
    def public_name(self) -> str:
        return public_agent_name(self.agent_id)

    def record_dict(self) -> dict:
        payload = asdict(self)
        if self.payload_version == 1:
            for key in (
                "payload_version",
                "task_summary",
                "intent",
                "work_scope",
                "details",
                "current_step",
            ):
                payload.pop(key, None)
        return payload

    def canonical_bytes(self) -> bytes:
        payload = {**self.record_dict(), "public_name": self.public_name}
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SignedAgentIdentity:
    record: AgentIdentityRecord
    signature: str

    def as_dict(self) -> dict:
        return {"record": self.record.record_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: dict) -> SignedAgentIdentity:
        if not isinstance(payload, dict):
            raise ValueError("signed identity payload must be an object")
        record = payload.get("record")
        signature = payload.get("signature")
        if not isinstance(record, dict) or not isinstance(signature, str) or not signature:
            raise ValueError("signed identity requires record and signature")
        normalized = dict(record)
        for key in ("work_scope", "details"):
            if key in normalized and isinstance(normalized[key], list):
                normalized[key] = tuple(normalized[key])
        return cls(AgentIdentityRecord(**normalized), signature)


def sign_identity_record(record: AgentIdentityRecord, private_key: str) -> str:
    signer = Ed25519PrivateKey.from_private_bytes(_decode(private_key))
    return _encode(signer.sign(record.canonical_bytes()))


def verify_identity_record(
    record: AgentIdentityRecord,
    signature: str,
    public_key: str,
) -> bool:
    verifier = Ed25519PublicKey.from_public_bytes(_decode(public_key))
    try:
        verifier.verify(_decode(signature), record.canonical_bytes())
    except (InvalidSignature, ValueError):
        return False
    return True

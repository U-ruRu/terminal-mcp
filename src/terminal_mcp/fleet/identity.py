from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from terminal_mcp.core.orchestration import public_agent_name


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

    def __post_init__(self):
        if not self.source_instance_id.strip():
            raise ValueError("source_instance_id must not be empty")
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.state not in {"active", "finished", "forced"}:
            raise ValueError("state must be active, finished or forced")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.state == "active" and self.ended_at is not None:
            raise ValueError("active identity must not have ended_at")
        if self.state != "active" and self.ended_at is None:
            raise ValueError("terminal identity requires ended_at")

    @property
    def public_name(self) -> str:
        return public_agent_name(self.agent_id)

    def canonical_bytes(self) -> bytes:
        payload = {**asdict(self), "public_name": self.public_name}
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


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

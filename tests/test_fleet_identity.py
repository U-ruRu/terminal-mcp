import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.identity import (
    AgentIdentityRecord,
    sign_identity_record,
    verify_identity_record,
)


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return encoded(private_raw), encoded(public_raw)


def record(**changes):
    values = {
        "source_instance_id": "server-a",
        "agent_id": "Alpha-01234567",
        "state": "active",
        "session_started_at": "2026-01-01T00:00:00.000Z",
        "expires_at": "2026-01-01T00:25:00.000Z",
        "updated_at": "2026-01-01T00:00:01.000Z",
        "revision": 1,
    }
    values.update(changes)
    return AgentIdentityRecord(**values)


def test_signed_identity_record_binds_identity_and_original_hard_clock():
    private, public = keypair()
    current = record()

    signature = sign_identity_record(current, private)

    assert current.public_name == "Alpha"
    assert verify_identity_record(current, signature, public) is True
    assert verify_identity_record(replace(current, revision=2), signature, public) is False
    assert verify_identity_record(
        replace(current, expires_at="2026-01-01T00:30:00.000Z"),
        signature,
        public,
    ) is False


def test_terminal_state_and_end_reason_are_signed():
    private, public = keypair()
    ended = record(
        state="finished",
        ended_at="2026-01-01T00:10:00.000Z",
        end_reason="explicit",
        revision=2,
    )
    signature = sign_identity_record(ended, private)

    assert verify_identity_record(ended, signature, public) is True
    assert verify_identity_record(replace(ended, end_reason="forced"), signature, public) is False


def test_distinct_private_suffixes_keep_same_public_name_distinct():
    one = record(agent_id="Bravo-01234567")
    two = record(agent_id="Bravo-89ABCDEF", source_instance_id="server-b")

    assert one.public_name == two.public_name == "Bravo"
    assert one.agent_id != two.agent_id


@pytest.mark.parametrize("revision", [0, -1])
def test_identity_revision_must_be_positive(revision):
    with pytest.raises(ValueError, match="revision"):
        record(revision=revision)


def test_active_and_terminal_end_state_invariants():
    with pytest.raises(ValueError, match="ended_at"):
        record(ended_at="2026-01-01T00:10:00.000Z")
    with pytest.raises(ValueError, match="requires ended_at"):
        record(state="forced")

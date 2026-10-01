import base64
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import build_fleet_config
from terminal_mcp.fleet.identity import (
    AgentIdentityRecord,
    SignedAgentIdentity,
    sign_identity_record,
)
from terminal_mcp.fleet.storage import FleetIdentityStore
from terminal_mcp.storage.sqlite import SqliteRepository


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


def record(revision=1, **changes):
    values = {
        "source_instance_id": "server-a",
        "agent_id": "Alpha-01234567",
        "state": "active",
        "session_started_at": "2026-01-01T00:00:00.000Z",
        "expires_at": "2026-01-01T00:25:00.000Z",
        "updated_at": "2026-01-01T00:00:01.000Z",
        "revision": revision,
    }
    values.update(changes)
    return AgentIdentityRecord(**values)


def envelope(private, revision=1, **changes):
    current = record(revision=revision, **changes)
    return SignedAgentIdentity(current, sign_identity_record(current, private))


def settings(**changes):
    values = {
        "fleet_instance_id": "",
        "fleet_signing_private_key": "",
        "fleet_peers_json": "[]",
        "fleet_replication_interval_sec": 5.0,
        "fleet_request_timeout_sec": 3.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_empty_fleet_configuration_is_disabled():
    assert build_fleet_config(settings()) is None


def test_synthetic_fleet_configuration_is_strict():
    private, _ = keypair()
    _, peer_public = keypair()
    config = build_fleet_config(
        settings(
            fleet_instance_id="server-a",
            fleet_signing_private_key=private,
            fleet_peers_json=(
                '[{"instance_id":"server-b","origin":"https://server-b.example.invalid",'
                f'"public_key":"{peer_public}","auth_token":"placeholder-peer-token"}}]'
            ),
        )
    )
    assert config is not None
    assert config.instance_id == "server-a"
    assert config.peers[0].origin == "https://server-b.example.invalid"

    with pytest.raises(ValueError, match="duplicate fleet instance_id"):
        build_fleet_config(
            settings(
                fleet_instance_id="server-a",
                fleet_signing_private_key=private,
                fleet_peers_json=(
                    '[{"instance_id":"server-a","origin":"https://server-a.example.invalid",'
                    f'"public_key":"{peer_public}","auth_token":"placeholder-peer-token"}}]'
                ),
            )
        )


@pytest.mark.asyncio
async def test_identity_store_deduplicates_and_rejects_stale_or_conflicting_revisions(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = FleetIdentityStore(repo.path)
    private, _ = keypair()

    first = envelope(private)
    assert await store.apply(first) == "applied"
    assert await store.apply(first) == "duplicate"

    conflict = envelope(private, revision=1, updated_at="2026-01-01T00:00:02.000Z")
    assert await store.apply(conflict) == "conflict"

    newer = envelope(private, revision=2, updated_at="2026-01-01T00:00:03.000Z")
    assert await store.apply(newer) == "applied"
    assert await store.apply(first) == "stale"

    loaded = await store.get("server-a", "Alpha-01234567")
    assert loaded == newer

    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 18


@pytest.mark.asyncio
async def test_peer_outbox_keeps_only_latest_revision_and_delivery_is_race_safe(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = FleetIdentityStore(repo.path)
    private, _ = keypair()

    first = envelope(private)
    second = envelope(private, revision=2, updated_at="2026-01-01T00:00:03.000Z")
    await store.enqueue(["server-b", "server-c"], first)
    await store.enqueue(["server-b"], second)

    pending_b = await store.pending("server-b")
    assert pending_b == [second]
    assert await store.outbox_count() == 2

    await store.delivery_result("server-b", first, error=None)
    assert await store.pending("server-b") == [second]

    await store.delivery_result("server-b", second, error="peer unavailable")
    assert await store.outbox_count("server-b") == 1
    with sqlite3.connect(repo.path) as db:
        attempt_count, last_error = db.execute(
            "SELECT attempt_count,last_error FROM fleet_peer_outbox "
            "WHERE peer_instance_id='server-b'"
        ).fetchone()
    assert attempt_count == 1
    assert last_error == "peer unavailable"

    await store.delivery_result("server-b", second, error=None)
    assert await store.outbox_count("server-b") == 0
    assert await store.outbox_count("server-c") == 1


def test_fleet_configuration_rejects_insecure_remote_origin_and_invalid_key_material():
    private, _ = keypair()
    _, peer_public = keypair()
    peer = (
        '[{"instance_id":"server-b","origin":"http://server-b.example.invalid",'
        f'"public_key":"{peer_public}","auth_token":"placeholder-peer-token"}}]'
    )
    with pytest.raises(ValueError, match="HTTPS"):
        build_fleet_config(
            settings(
                fleet_instance_id="server-a",
                fleet_signing_private_key=private,
                fleet_peers_json=peer,
            )
        )

    loopback = build_fleet_config(
        settings(
            fleet_instance_id="server-a",
            fleet_signing_private_key=private,
            fleet_peers_json=peer.replace(
                "http://server-b.example.invalid", "http://127.0.0.1:9123"
            ),
        )
    )
    assert loopback is not None
    assert loopback.peers[0].origin == "http://127.0.0.1:9123"

    with pytest.raises(ValueError, match="32 bytes"):
        build_fleet_config(
            settings(
                fleet_instance_id="server-a",
                fleet_signing_private_key="bad-key",
                fleet_peers_json="[]",
            )
        )

    bad_peer_key = (
        '[{"instance_id":"server-b","origin":"https://server-b.example.invalid",'
        '"public_key":"bad-key","auth_token":"placeholder-peer-token"}]'
    )
    with pytest.raises(ValueError, match="32 bytes"):
        build_fleet_config(
            settings(
                fleet_instance_id="server-a",
                fleet_signing_private_key=private,
                fleet_peers_json=bad_peer_key,
            )
        )


@pytest.mark.asyncio
async def test_identity_store_preserves_originating_hard_clock_and_terminal_state(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = FleetIdentityStore(repo.path)
    private, _ = keypair()

    first = envelope(private)
    assert await store.apply(first) == "applied"

    reminted = envelope(
        private,
        revision=2,
        expires_at="2026-01-01T00:30:00.000Z",
        updated_at="2026-01-01T00:00:02.000Z",
    )
    assert await store.apply(reminted) == "conflict"
    assert await store.get("server-a", "Alpha-01234567") == first

    finished = envelope(
        private,
        revision=2,
        state="finished",
        ended_at="2026-01-01T00:10:00.000Z",
        end_reason="explicit",
        updated_at="2026-01-01T00:10:00.000Z",
    )
    assert await store.apply(finished) == "applied"

    resurrected = envelope(
        private,
        revision=3,
        state="active",
        ended_at=None,
        end_reason=None,
        updated_at="2026-01-01T00:11:00.000Z",
    )
    assert await store.apply(resurrected) == "conflict"
    assert await store.get("server-a", "Alpha-01234567") == finished

import base64
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.identity import (
    AgentIdentityRecord,
    SignedAgentIdentity,
    sign_identity_record,
)
from terminal_mcp.fleet.replication import FleetReplicationService
from terminal_mcp.fleet.storage import FleetIdentityStore
from terminal_mcp.http.fleet import build_fleet_router
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return (
        encoded(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        encoded(
            private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ),
    )


def record(source, agent_id, private, revision=1, state="active", ended_at=None):
    started = "2026-01-01T00:00:00.000Z"
    current = AgentIdentityRecord(
        source_instance_id=source,
        agent_id=agent_id,
        state=state,
        session_started_at=started,
        expires_at=utc_text(parse_utc(started) + timedelta(minutes=25)),
        updated_at=ended_at or started,
        revision=revision,
        ended_at=ended_at,
        end_reason="explicit" if ended_at else None,
    )
    return SignedAgentIdentity(current, sign_identity_record(current, private))


async def runtime(tmp_path, transport=None):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    agents = AgentStore(repo.path)
    identities = FleetIdentityStore(repo.path)
    private_a, public_a = keypair()
    private_b, public_b = keypair()
    peer = FleetPeer(
        "server-b",
        "https://server-b.example.invalid",
        public_b,
        "placeholder-peer-token",
    )
    config = FleetConfig("server-a", private_a, (peer,), 1.0, 1.0)

    def client_factory():
        return httpx.AsyncClient(
            transport=transport,
            timeout=1.0,
        )

    replication = FleetReplicationService(
        config,
        identities,
        agents,
        max_session_seconds=1500,
        client_factory=client_factory if transport else None,
    )
    return repo, agents, identities, replication, private_a, public_a, private_b, public_b


@pytest.mark.asyncio
async def test_local_session_replication_preserves_original_hard_clock_and_latest_outbox(tmp_path):
    _, agents, identities, replication, _, _, _, _ = await runtime(tmp_path)
    started = "2026-01-01T00:00:00.000Z"
    await agents.create_session(
        "Alpha-01234567",
        "synthetic task",
        "synthetic intent",
        [],
        ["synthetic step"],
        1,
        started,
    )

    first = await replication.sync_local_session("Alpha-01234567")
    duplicate = await replication.sync_local_session("Alpha-01234567")

    assert first is not None
    assert duplicate == first
    assert first.record.expires_at == "2026-01-01T00:25:00.000Z"
    assert first.record.revision == 1
    assert await identities.outbox_count("server-b") == 1

    ended = "2026-01-01T00:10:00.000Z"
    assert await agents.finish("Alpha-01234567", ended) is True
    terminal = await replication.sync_local_session("Alpha-01234567")
    assert terminal is not None
    assert terminal.record.state == "finished"
    assert terminal.record.revision == 2
    assert terminal.record.expires_at == first.record.expires_at
    pending = await identities.pending("server-b")
    assert pending == [terminal]


@pytest.mark.asyncio
async def test_receive_requires_configured_source_valid_signature_and_monotonic_revision(tmp_path):
    _, _, identities, replication, _, _, private_b, _ = await runtime(tmp_path)
    first = record("server-b", "Bravo-89ABCDEF", private_b)

    assert await replication.receive(first, authenticated_peer_id="server-b") == "applied"
    assert await replication.receive(first, authenticated_peer_id="server-b") == "duplicate"

    with pytest.raises(ValueError, match="does not match"):
        await replication.receive(first, authenticated_peer_id="server-c")

    attacker_private, _ = keypair()
    forged = record("server-b", "Bravo-89ABCDEF", attacker_private, revision=2)
    with pytest.raises(ValueError, match="signature"):
        await replication.receive(forged, authenticated_peer_id="server-b")

    loaded = await identities.get("server-b", "Bravo-89ABCDEF")
    assert loaded == first


@pytest.mark.asyncio
async def test_flush_is_best_effort_and_retries_partitioned_peer(tmp_path, monkeypatch):
    attempts = 0

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        assert request.headers["x-terminal-mcp-peer"] == "server-a"
        assert request.headers["authorization"] == "Bearer placeholder-peer-token"
        if attempts == 1:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "status": "applied"})

    transport = httpx.MockTransport(handler)
    _, agents, identities, replication, _, _, _, _ = await runtime(tmp_path, transport)
    monkeypatch.setattr(replication, "kick", lambda: None)
    await agents.create_session(
        "Charlie-01234567",
        "synthetic task",
        "synthetic intent",
        [],
        ["synthetic step"],
        1,
        "2026-01-01T00:00:00.000Z",
    )
    await replication.sync_local_session("Charlie-01234567")

    await replication.flush_once()
    assert await identities.outbox_count("server-b") == 1

    await replication.flush_once()
    assert await identities.outbox_count("server-b") == 0
    assert attempts == 2


@pytest.mark.asyncio
async def test_pull_catches_up_signed_peer_identity_after_partition(tmp_path):
    private_b, public_b = keypair()
    peer_identity = record("server-b", "Delta-89ABCDEF", private_b)

    def handler(request: httpx.Request):
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True, "identities": [peer_identity.as_dict()]})

    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    identities = FleetIdentityStore(repo.path)
    agents = AgentStore(repo.path)
    private_a, _ = keypair()
    config = FleetConfig(
        "server-a",
        private_a,
        (
            FleetPeer(
                "server-b",
                "https://server-b.example.invalid",
                public_b,
                "placeholder-peer-token",
            ),
        ),
        1.0,
        1.0,
    )
    replication = FleetReplicationService(
        config,
        identities,
        agents,
        max_session_seconds=1500,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await replication.pull_once()

    assert await identities.get("server-b", "Delta-89ABCDEF") == peer_identity


@pytest.mark.asyncio
async def test_internal_peer_api_authenticates_channel_and_record_source(tmp_path):
    _, _, identities, replication, _, _, private_b, _ = await runtime(tmp_path)
    app = FastAPI()
    app.include_router(build_fleet_router(replication))
    transport = httpx.ASGITransport(app=app)
    item = record("server-b", "Echo-89ABCDEF", private_b)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://server-a.example.invalid"
    ) as client:
        unauthorized = await client.post("/internal/fleet/identities", json=item.as_dict())
        assert unauthorized.status_code == 401

        headers = {
            "Authorization": "Bearer placeholder-peer-token",
            "X-Terminal-MCP-Peer": "server-b",
        }
        accepted = await client.post(
            "/internal/fleet/identities",
            headers=headers,
            json=item.as_dict(),
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "applied"

        listing = await client.get("/internal/fleet/identities", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["identities"] == [item.as_dict()]

        wrong_source = record("server-c", "Foxtrot-01234567", private_b)
        rejected = await client.post(
            "/internal/fleet/identities",
            headers=headers,
            json=wrong_source.as_dict(),
        )
        assert rejected.status_code == 400

    assert await identities.get("server-b", "Echo-89ABCDEF") == item


@pytest.mark.asyncio
async def test_sync_before_service_start_persists_outbox_without_background_kick(tmp_path):
    _, agents, identities, replication, _, _, _, _ = await runtime(tmp_path)
    await agents.create_session(
        "Golf-01234567",
        "synthetic task",
        "synthetic intent",
        [],
        ["synthetic step"],
        1,
        "2026-01-01T00:00:00.000Z",
    )

    await replication.sync_local_session("Golf-01234567")

    assert replication._task is None
    assert replication._kick_task is None
    assert await identities.outbox_count("server-b") == 1


@pytest.mark.asyncio
async def test_terminal_service_schedules_replication_after_admission_and_finish(tmp_path):
    class ReplicationSpy:
        def __init__(self):
            self.agent_ids = []

        def schedule_local_sync(self, agent_id):
            self.agent_ids.append(agent_id)

    from terminal_mcp.core.service import TerminalService
    from terminal_mcp.terminal.linux import LinuxTerminalAdapter

    repo = SqliteRepository(tmp_path / "service.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    replication = ReplicationSpy()
    service = TerminalService(repo, terminal, 5000, fleet_replication=replication)
    plan = {
        "task_summary": "synthetic admission",
        "intent": "verify replication hook",
        "details": ["confirm identity"],
        "work_scope": ["synthetic"],
    }

    proposal = await service.agent_start(**plan)
    assert proposal["admission_required"] is True
    assert replication.agent_ids == []

    agent_id = proposal["proposed_agent_id"]
    admitted = await service.agent_start(agent_id=agent_id, **plan)
    assert admitted["ok"] is True
    assert replication.agent_ids == [agent_id]

    finished = await service.agent_finish(agent_id)
    assert finished["finished"] is True
    assert replication.agent_ids == [agent_id, agent_id]

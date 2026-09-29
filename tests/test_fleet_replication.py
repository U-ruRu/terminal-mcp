import asyncio
import base64
import json
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
async def test_three_peer_partition_rejoin_preserves_one_global_session_clock(
    tmp_path, monkeypatch
):
    private_a, _ = keypair()
    _, public_b = keypair()
    _, public_c = keypair()
    peer_b = FleetPeer(
        "server-b",
        "https://server-b.example.invalid",
        public_b,
        "placeholder-peer-token-b",
    )
    peer_c = FleetPeer(
        "server-c",
        "https://server-c.example.invalid",
        public_c,
        "placeholder-peer-token-c",
    )
    delivered: dict[str, list[dict]] = {"server-b": [], "server-c": []}
    partition_c = True

    def handler(request: httpx.Request):
        nonlocal partition_c
        peer_id = request.url.host.split(".")[0]
        assert request.method == "POST"
        assert request.headers["x-terminal-mcp-peer"] == "server-a"
        if peer_id == "server-c" and partition_c:
            return httpx.Response(503, json={"ok": False})
        delivered[peer_id].append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "status": "applied"})

    repo = SqliteRepository(tmp_path / "three-peer.sqlite3")
    await repo.initialize()
    agents = AgentStore(repo.path)
    identities = FleetIdentityStore(repo.path)
    replication = FleetReplicationService(
        FleetConfig("server-a", private_a, (peer_b, peer_c), 1.0, 1.0),
        identities,
        agents,
        max_session_seconds=1500,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=1.0,
        ),
    )
    monkeypatch.setattr(replication, "kick", lambda: None)

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
    envelope = await replication.sync_local_session("Alpha-01234567")
    assert envelope is not None
    assert envelope.record.session_started_at == started
    assert envelope.record.expires_at == "2026-01-01T00:25:00.000Z"

    await replication.flush_once()
    assert len(delivered["server-b"]) == 1
    assert delivered["server-c"] == []
    assert await identities.outbox_count("server-b") == 0
    assert await identities.outbox_count("server-c") == 1

    partition_c = False
    await replication.flush_once()
    assert len(delivered["server-b"]) == 1
    assert len(delivered["server-c"]) == 1
    assert await identities.outbox_count("server-c") == 0

    delivered_b = SignedAgentIdentity.from_dict(delivered["server-b"][0])
    delivered_c = SignedAgentIdentity.from_dict(delivered["server-c"][0])
    assert delivered_b.record.agent_id == delivered_c.record.agent_id == "Alpha-01234567"
    assert delivered_b.record.revision == delivered_c.record.revision == 1
    assert delivered_b.record.session_started_at == delivered_c.record.session_started_at == started
    assert (
        delivered_b.record.expires_at
        == delivered_c.record.expires_at
        == "2026-01-01T00:25:00.000Z"
    )


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
            self.config = type("Config", (), {"instance_id": "server-a"})()
            self.origin_finish_handler = None

        async def resolve_session(self, agent_id):
            return None

        def bind_origin_finish_handler(self, handler):
            self.origin_finish_handler = handler

        async def queue_finish(self, agent_id):
            return False

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


def v2_record(
    source,
    agent_id,
    private,
    *,
    started,
    expires,
    revision=1,
    state="active",
    ended_at=None,
    end_reason=None,
    task_summary="synthetic shared task",
    intent="continue shared work",
    work_scope=("repo:synthetic",),
    details=("inspect peer state", "finish safely"),
    current_step=1,
    updated_at=None,
):
    current = AgentIdentityRecord(
        source_instance_id=source,
        agent_id=agent_id,
        state=state,
        session_started_at=started,
        expires_at=expires,
        updated_at=updated_at or ended_at or started,
        revision=revision,
        ended_at=ended_at,
        end_reason=end_reason,
        payload_version=2,
        task_summary=task_summary,
        intent=intent,
        work_scope=tuple(work_scope),
        details=tuple(details),
        current_step=current_step,
    )
    return SignedAgentIdentity(current, sign_identity_record(current, private))


async def fleet_service(tmp_path, instance_id, private, peers, *, transport=None):
    from terminal_mcp.core.service import TerminalService
    from terminal_mcp.terminal.linux import LinuxTerminalAdapter

    repo = SqliteRepository(tmp_path / f"{instance_id}.sqlite3")
    await repo.initialize()
    agents = AgentStore(repo.path)
    identities = FleetIdentityStore(repo.path)
    config = FleetConfig(instance_id, private, tuple(peers), 1.0, 1.0)

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
        client_factory=client_factory if transport is not None else None,
    )
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000, fleet_replication=replication)
    return repo, agents, identities, replication, terminal, service


@pytest.mark.asyncio
async def test_concurrent_lookup_never_returns_stale_active_after_newer_terminal_revision(
    tmp_path,
):
    private_a, public_a = keypair()
    private_b, public_b = keypair()
    private_c, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    peer_b = FleetPeer(
        "server-b",
        "https://server-b.example.invalid",
        public_b,
        "placeholder-peer-token-b",
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    ended = utc_text(parse_utc(started) + timedelta(minutes=5))
    agent_id = "Race-01234567"
    active_v1 = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=1,
        state="active",
    )
    finished_v2 = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=2,
        state="finished",
        ended_at=ended,
        end_reason="explicit",
    )

    newer_applied = asyncio.Event()

    async def handler(request: httpx.Request):
        if request.url.host == "server-a.example.invalid":
            return httpx.Response(
                200,
                json={"ok": True, "identities": [finished_v2.as_dict()]},
            )
        if request.url.host == "server-b.example.invalid":
            await newer_applied.wait()
            return httpx.Response(
                200,
                json={"ok": True, "identities": [active_v1.as_dict()]},
            )
        return httpx.Response(404)

    repo = SqliteRepository(tmp_path / "race.sqlite3")
    await repo.initialize()
    agents = AgentStore(repo.path)
    identities = FleetIdentityStore(repo.path)
    config = FleetConfig(
        "server-c",
        private_c,
        (peer_a, peer_b),
        1.0,
        1.0,
    )
    replication = FleetReplicationService(
        config,
        identities,
        agents,
        max_session_seconds=1500,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=1.0,
        ),
    )
    original_receive = replication.receive
    hold_newer = asyncio.Event()

    async def controlled_receive(envelope, authenticated_peer_id=None):
        status = await original_receive(
            envelope,
            authenticated_peer_id=authenticated_peer_id,
        )
        if envelope.record.revision == 2:
            newer_applied.set()
            await hold_newer.wait()
        return status

    replication.receive = controlled_receive

    resolution = await replication.resolve_session(agent_id)

    assert resolution is not None
    assert resolution["state"] == "finished"
    assert resolution["ended_at"] == ended
    stored = await identities.get("server-a", agent_id)
    assert stored is not None
    assert stored.record.revision == 2
    assert stored.record.state == "finished"
    await replication.stop()


@pytest.mark.asyncio
async def test_service_attaches_signed_foreign_session_with_original_plan_and_clock(tmp_path):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    repo, agents, identities, replication, terminal, service = await fleet_service(
        tmp_path, "server-b", private_b, [peer_a]
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    agent_id = "Alpha-01234567"
    envelope = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        task_summary="origin task",
        intent="origin intent",
        work_scope=("repo:shared",),
        details=("origin step one", "origin step two"),
        current_step=2,
    )
    assert await replication.receive(envelope) == "applied"

    attached = await service.agent_start(agent_id=agent_id)

    assert attached["ok"] is True
    assert attached["self"]["task_summary"] == "origin task"
    assert attached["self"]["intent"] == "origin intent"
    assert attached["self"]["work_scope"] == ["repo:shared"]
    assert attached["self"]["current_step"] == 2
    session = await agents.get_session(agent_id)
    assert session["registered_at"] == started
    assert session["global_expires_at"] == expires
    assert session["source_instance_id"] == "server-a"
    assert await identities.get("server-b", agent_id) is None
    assert await identities.get("server-a", agent_id) == envelope
    await replication.stop()
    await terminal.stop()


@pytest.mark.asyncio
async def test_newer_active_foreign_identity_refreshes_attached_plan_without_resetting_clock(
    tmp_path,
):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    _, agents, _, replication, terminal, service = await fleet_service(
        tmp_path, "server-b", private_b, [peer_a]
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    agent_id = "Bravo-01234567"
    initial = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=1,
        task_summary="initial shared task",
        intent="inspect initial state",
        work_scope=("repo:shared",),
        details=("initial step", "finish safely"),
        current_step=1,
    )
    assert await replication.receive(initial) == "applied"
    assert (await service.agent_start(agent_id=agent_id))["ok"] is True
    before = await agents.get_session(agent_id)

    updated_at = utc_text(parse_utc(started) + timedelta(seconds=30))
    updated = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=2,
        updated_at=updated_at,
        task_summary="updated shared task",
        intent="continue from authoritative update",
        work_scope=("repo:shared", "tests"),
        details=("inspect update", "run regression", "finish safely"),
        current_step=2,
    )
    assert await replication.receive(updated) == "applied"

    stale = await agents.get_session(agent_id)
    assert stale["intent"] == "inspect initial state"
    assert stale["current_step"] == 1

    coordinated = await service.coordinate(agent_id)
    assert coordinated["ok"] is True
    assert coordinated["intent"] == "continue from authoritative update"
    assert coordinated["step"] == 2
    assert coordinated["detail"] == "run regression"

    after = await agents.get_session(agent_id)
    assert after["task_summary"] == "updated shared task"
    assert after["intent"] == "continue from authoritative update"
    assert after["work_scope"] == ["repo:shared", "tests"]
    assert after["details"] == ["inspect update", "run regression", "finish safely"]
    assert after["current_step"] == 2
    assert after["registered_at"] == before["registered_at"] == started
    assert after["global_expires_at"] == before["global_expires_at"] == expires
    assert after["source_instance_id"] == before["source_instance_id"] == "server-a"
    assert await agents.latest_task_at(agent_id) == updated_at

    observed = await service.agents(agent_id=agent_id, target="Bravo", show_details=True)
    assert observed["ok"] is True
    assert observed["sessions"][0]["intent"] == "continue from authoritative update"
    assert observed["sessions"][0]["current_step"] == 2
    assert observed["sessions"][0]["task_summary"] == "updated shared task"

    ended = utc_text(parse_utc(started) + timedelta(minutes=1))
    finished = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=3,
        state="finished",
        ended_at=ended,
        end_reason="explicit",
        task_summary="updated shared task",
        intent="continue from authoritative update",
        work_scope=("repo:shared", "tests"),
        details=("inspect update", "run regression", "finish safely"),
        current_step=2,
    )
    assert await replication.receive(finished) == "applied"
    terminal_result = await service.coordinate(agent_id)
    assert terminal_result["ok"] is False
    assert terminal_result["return_to_chat"] is True
    assert terminal_result["session_status"] == "finished"

    final = await agents.get_session(agent_id)
    assert final["state"] == "finished"
    assert final["registered_at"] == started
    assert final["global_expires_at"] == expires
    assert final["ended_at"] == ended
    assert final["end_reason"] == "explicit"

    await replication.stop()
    await terminal.stop()


@pytest.mark.asyncio
async def test_unknown_foreign_identity_partition_fails_closed_without_local_session(tmp_path):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )

    def unavailable(_request):
        return httpx.Response(503, json={"ok": False})

    repo, agents, _, _, terminal, service = await fleet_service(
        tmp_path,
        "server-b",
        private_b,
        [peer_a],
        transport=httpx.MockTransport(unavailable),
    )
    agent_id = "Bravo-89ABCDEF"

    result = await service.agent_start(
        agent_id=agent_id,
        task_summary="must not remint",
        intent="preserve clock",
        details=["preserve clock"],
    )

    assert result["ok"] is False
    assert result["foreign_identity_unavailable"] is True
    assert result["retryable"] is True
    assert await agents.get_session(agent_id) is None
    await service.fleet_replication.stop()
    await terminal.stop()


@pytest.mark.asyncio
async def test_idle_foreign_attachment_reattaches_without_resetting_hard_clock(tmp_path):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    _, agents, _, replication, terminal, service = await fleet_service(
        tmp_path, "server-b", private_b, [peer_a]
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    agent_id = "Charlie-01234567"
    envelope = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
    )
    await replication.receive(envelope)
    assert (await service.agent_start(agent_id=agent_id))["ok"] is True
    before = await agents.get_session(agent_id)

    assert await agents.expire(agent_id, reason="idle_timeout") is True
    resumed = await service.agent_start(agent_id=agent_id)
    after = await agents.get_session(agent_id)

    assert resumed["ok"] is True
    assert after["state"] == "active"
    assert after["registered_at"] == before["registered_at"] == started
    assert after["global_expires_at"] == before["global_expires_at"] == expires
    assert after["source_instance_id"] == "server-a"
    await replication.stop()
    await terminal.stop()


@pytest.mark.asyncio
async def test_expired_foreign_identity_returns_to_chat_without_attachment(tmp_path):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    _, agents, _, replication, terminal, service = await fleet_service(
        tmp_path, "server-b", private_b, [peer_a]
    )
    started = utc_text(parse_utc(utc_text()) - timedelta(minutes=30))
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    agent_id = "Delta-89ABCDEF"
    await replication.receive(
        v2_record("server-a", agent_id, private_a, started=started, expires=expires)
    )

    result = await service.agent_start(agent_id=agent_id)

    assert result["ok"] is False
    assert result["return_to_chat"] is True
    assert result["session_end_reason"] == "max_session_duration"
    assert await agents.get_session(agent_id) is None
    await replication.stop()
    await terminal.stop()


@pytest.mark.asyncio
async def test_foreign_finish_retries_partition_then_origin_finishes_authoritative_session(
    tmp_path,
):
    private_a, public_a = keypair()
    private_b, public_b = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    peer_b = FleetPeer(
        "server-b",
        "https://server-b.example.invalid",
        public_b,
        "placeholder-peer-token-b",
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    agent_id = "Echo-01234567"

    _, agents_a, identities_a, replication_a, terminal_a, service_a = await fleet_service(
        tmp_path / "origin", "server-a", private_a, [peer_b]
    )
    await agents_a.create_session(
        agent_id,
        "origin task",
        "origin intent",
        ["repo:shared"],
        ["origin step"],
        1,
        started,
        source_instance_id="server-a",
        global_expires_at=expires,
    )
    await replication_a.sync_local_session(agent_id)
    origin_envelope = await identities_a.get("server-a", agent_id)
    assert origin_envelope is not None

    attempts = 0

    async def handler(request: httpx.Request):
        nonlocal attempts
        if request.url.path == "/internal/fleet/session-finish":
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"ok": False})
            body = json.loads(request.content)
            changed = await replication_a.receive_finish(
                body["agent_id"],
                body["ended_at"],
                body["reason"],
                authenticated_peer_id="server-b",
            )
            return httpx.Response(200, json={"ok": True, "changed": changed})
        return httpx.Response(404)

    _, agents_b, identities_b, replication_b, terminal_b, service_b = await fleet_service(
        tmp_path / "attached",
        "server-b",
        private_b,
        [peer_a],
        transport=httpx.MockTransport(handler),
    )
    assert await replication_b.receive(origin_envelope) == "applied"
    assert (await service_b.agent_start(agent_id=agent_id))["ok"] is True

    finished = await service_b.agent_finish(agent_id)
    assert finished["finished"] is True
    assert (await agents_b.get_session(agent_id))["state"] == "finished"
    assert await identities_b.finish_outbox_count() == 1

    await replication_b.flush_finishes_once()
    assert await identities_b.finish_outbox_count() == 1
    await replication_b.flush_finishes_once()
    assert await identities_b.finish_outbox_count() == 0
    assert attempts == 2

    origin_session = await agents_a.get_session(agent_id)
    assert origin_session["state"] == "finished"
    terminal_envelope = await identities_a.get("server-a", agent_id)
    assert terminal_envelope.record.state == "finished"
    assert terminal_envelope.record.session_started_at == started
    assert terminal_envelope.record.expires_at == expires
    await replication_b.stop()
    await replication_a.stop()
    await terminal_b.stop()
    await terminal_a.stop()

@pytest.mark.asyncio
async def test_finished_foreign_identity_overrides_stale_local_active_attachment(tmp_path):
    private_a, public_a = keypair()
    private_b, _ = keypair()
    peer_a = FleetPeer(
        "server-a",
        "https://server-a.example.invalid",
        public_a,
        "placeholder-peer-token-a",
    )
    _, agents, _, replication, terminal, service = await fleet_service(
        tmp_path, "server-b", private_b, [peer_a]
    )
    started = utc_text()
    expires = utc_text(parse_utc(started) + timedelta(minutes=25))
    ended = utc_text(parse_utc(started) + timedelta(minutes=1))
    agent_id = "Golf-01234567"

    active = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=1,
    )
    assert await replication.receive(active) == "applied"
    assert (await service.agent_start(agent_id=agent_id))["ok"] is True
    assert (await agents.get_session(agent_id))["state"] == "active"

    finished = v2_record(
        "server-a",
        agent_id,
        private_a,
        started=started,
        expires=expires,
        revision=2,
        state="finished",
        ended_at=ended,
        end_reason="explicit",
    )
    assert await replication.receive(finished) == "applied"
    # Replication storage is authoritative, but the materialized local shadow
    # remains active until the next local tool/admission gate reconciles it.
    assert (await agents.get_session(agent_id))["state"] == "active"

    result = await service.agent_start(
        agent_id=agent_id,
        task_summary="must not resurrect",
        intent="honor authoritative finish",
        details=["return to chat"],
    )

    assert result["ok"] is False
    assert result["return_to_chat"] is True
    assert result["session_status"] == "finished"
    assert result["session_end_reason"] == "explicit"
    local = await agents.get_session(agent_id)
    assert local["state"] == "finished"
    assert local["ended_at"] == ended
    assert local["end_reason"] == "explicit"
    assert local["registered_at"] == started
    assert local["global_expires_at"] == expires

    await replication.stop()
    await terminal.stop()

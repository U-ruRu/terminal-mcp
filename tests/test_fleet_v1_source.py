import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terminal_mcp.fleet.protocol import (
    FLEET_PROTOCOL_MAJOR,
    FleetProtocolError,
    canonical_event_id,
    validate_capabilities,
    validate_protocol_major,
)
from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.http.fleet_v1 import build_fleet_v1_source_router
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


def test_protocol_validation_and_event_id_are_deterministic():
    first = canonical_event_id("fleet-a", "node-a", "abc123", 7)
    second = canonical_event_id("fleet-a", "node-a", "abc123", 7)

    assert first == second
    assert len(first) == 64
    assert validate_protocol_major(FLEET_PROTOCOL_MAJOR) == 1
    assert validate_capabilities(["fleet.source.v1", "fleet.source.v1"]) == ("fleet.source.v1",)

    with pytest.raises(FleetProtocolError, match="unsupported"):
        validate_protocol_major(2)
    with pytest.raises(FleetProtocolError, match="invalid fleet capability"):
        validate_capabilities(["bad capability"])


@pytest.mark.asyncio
async def test_node_meta_persists_and_rotates_generation_on_runtime_regression(tmp_path):
    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    first = await store.initialize()
    assert first.served_high_water == 0
    assert store.sqlite_diagnostics.role == "fleet_node_meta"

    advanced, rotated = await store.observe_journal(11)
    assert rotated is False
    assert advanced.source_stream_generation == first.source_stream_generation
    assert advanced.served_high_water == 11

    restarted = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    stable = await restarted.initialize()
    assert stable.source_stream_generation == first.source_stream_generation
    assert stable.served_high_water == 11

    reset, rotated = await restarted.observe_journal(3)
    assert rotated is True
    assert reset.source_stream_generation != first.source_stream_generation
    assert reset.served_high_water == 3

    replacement = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-b")
    with pytest.raises(RuntimeError, match="does not match"):
        await replacement.initialize()


@pytest.mark.asyncio
async def test_node_meta_concurrent_stable_observations_do_not_contend(tmp_path):
    import asyncio

    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    await store.initialize()
    advanced, rotated = await store.observe_journal(23)
    assert rotated is False

    results = await asyncio.gather(*(store.observe_journal(23) for _ in range(100)))

    assert all(not item[1] for item in results)
    assert all(item[0].served_high_water == 23 for item in results)
    assert all(
        item[0].source_stream_generation == advanced.source_stream_generation
        for item in results
    )


@pytest.mark.asyncio
async def test_source_wraps_persistent_events_and_complete_snapshot(tmp_path):
    runtime_path = tmp_path / "terminal.sqlite3"
    repo = SqliteRepository(runtime_path, tmp_path / "output.sqlite3")
    await repo.initialize()
    persistent = PersistentAgentStore(runtime_path)
    slot = await persistent.create_slot(
        "logical-1",
        "Agent One",
        "A1B2",
        authority_node_id="node-a",
        now="2026-01-01T00:00:00Z",
    )
    assert slot.slot_revision == 1

    journal = EventJournalStore(runtime_path)
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
    )
    await meta.initialize()
    source = FleetSourceService(runtime_path, journal, meta)

    page = await source.events(since=0, limit=100)
    logical_events = [item for item in page["events"] if item["entity_type"] == "logical_agent"]
    assert len(logical_events) == 1
    event = logical_events[0]
    assert event["event_type"] == "logical_agent.created"
    assert event["entity_id"] == "logical-1"
    assert event["entity_revision"] == 1
    assert event["authority_node_id"] == "node-a"
    assert event["authority_epoch"] == 1
    assert event["event_id"] == canonical_event_id(
        "fleet-a",
        "node-a",
        page["source_stream_generation"],
        event["source_seq"],
    )

    snapshot = await source.snapshot()
    assert snapshot["replace"] is True
    assert snapshot["barrier_source_seq"] >= event["source_seq"]
    assert "logical_agent" in snapshot["complete_entity_types"]
    logical = next(item for item in snapshot["entities"] if item["entity_type"] == "logical_agent")
    assert logical["entity_id"] == "logical-1"
    assert logical["entity_revision"] == 1
    assert logical["payload"]["display_name"] == "Agent One"
    assert logical["payload"]["state"] == "suspended"


@pytest.mark.asyncio
async def test_source_generation_mismatch_and_retention_gap_require_reset(tmp_path):
    runtime_path = tmp_path / "terminal.sqlite3"
    repo = SqliteRepository(runtime_path, tmp_path / "output.sqlite3")
    await repo.initialize()
    journal = EventJournalStore(runtime_path, max_rows=2)
    for index in range(1, 5):
        await journal.append(
            "task.changed",
            "task",
            f"scope/TASK-{index}",
            payload={"state": "ready"},
            created_at=f"2026-01-01T00:00:0{index}Z",
        )

    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
    )
    initial = await meta.initialize()
    source = FleetSourceService(runtime_path, journal, meta)

    wrong = await source.events(
        since=0,
        source_stream_generation="wrong-generation",
    )
    assert wrong["reset_required"] is True
    assert wrong["reset_reason"] == "source_stream_generation_changed"
    assert wrong["events"] == []

    gap = await source.events(
        since=0,
        source_stream_generation=initial.source_stream_generation,
    )
    assert gap["gap"] is True
    assert gap["reset_required"] is True
    assert gap["reset_reason"] == "retention_gap"


class _Peer:
    instance_id = "peer-a"


class _Replication:
    def authenticate(self, peer_id, authorization):
        if peer_id == "peer-a" and authorization == "Bearer token":
            return _Peer()
        return None


class _Source:
    async def manifest(self):
        return {"protocol_major": 1}

    async def events(self, **kwargs):
        return {"events": [], **kwargs}

    async def snapshot(self):
        return {"replace": True}


@pytest.mark.asyncio
async def test_source_http_surface_reuses_fleet_peer_authentication():
    app = FastAPI()
    app.include_router(build_fleet_v1_source_router(_Source(), _Replication()))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/internal/fleet/v1/source/manifest")
        assert unauthorized.status_code == 401

        headers = {
            "x-terminal-mcp-peer": "peer-a",
            "authorization": "Bearer token",
        }
        manifest = await client.get("/internal/fleet/v1/source/manifest", headers=headers)
        assert manifest.status_code == 200
        assert manifest.json()["protocol_major"] == 1

        events = await client.get(
            "/internal/fleet/v1/source/events",
            headers=headers,
            params={"since": 4, "limit": 17, "source_stream_generation": "gen-a"},
        )
        assert events.status_code == 200
        assert events.json()["since"] == 4
        assert events.json()["limit"] == 17
        assert events.json()["source_stream_generation"] == "gen-a"


def test_fleet_v1_source_settings_are_default_off_and_validate_identity(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from terminal_mcp.config import Settings

    for key in (
        "TERMINAL_MCP_FLEET_V1_SOURCE_ENABLED",
        "TERMINAL_MCP_FLEET_V1_AUTHORITY_ENABLED",
        "TERMINAL_MCP_FLEET_V1_PROJECTION_ENABLED",
        "TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED",
        "TERMINAL_MCP_FLEET_ID",
        "TERMINAL_MCP_FLEET_INSTANCE_ID",
        "TERMINAL_MCP_FLEET_NODE_ID",
        "TERMINAL_MCP_FLEET_SIGNING_PRIVATE_KEY",
        "TERMINAL_MCP_FLEET_PEERS_JSON",
        "TERMINAL_MCP_FLEET_NODE_META_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    defaults = Settings(_env_file=None, database_path=tmp_path / "runtime.sqlite3")
    assert defaults.fleet_v1_source_enabled is False
    assert defaults.fleet_id == ""
    assert defaults.fleet_node_id == ""
    assert defaults.effective_fleet_node_meta_path() == tmp_path / "fleet-node-meta.sqlite3"

    with pytest.raises(ValidationError, match="fleet_id is required"):
        Settings(
            _env_file=None,
            fleet_v1_source_enabled=True,
            fleet_instance_id="node-a",
            fleet_signing_private_key="placeholder",
        )

    enabled = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        fleet_v1_source_enabled=True,
        fleet_id="fleet-a",
        fleet_instance_id="legacy-node-a",
        fleet_node_id="node-a",
        fleet_signing_private_key="placeholder",
        fleet_node_meta_path=tmp_path / "node-meta.sqlite3",
    )
    assert enabled.effective_fleet_node_id() == "node-a"
    assert enabled.effective_fleet_node_meta_path() == tmp_path / "node-meta.sqlite3"


@pytest.mark.asyncio
async def test_source_projects_persistent_attachment_presence_and_obligation(tmp_path):
    runtime_path = tmp_path / "terminal.sqlite3"
    repo = SqliteRepository(runtime_path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(runtime_path)
    await store.create_slot(
        "logical-1", "Agent One", "A1B2", authority_node_id="node-a"
    )
    armed, _ = await store.arm_slot(
        "logical-1", 120, expected_revision=1
    )
    slot, session = await store.start_session(
        selector="A1B2",
        work_session_id="ws-1",
        expected_revision=armed.slot_revision,
        principal_id="client-1",
        auth_generation=1,
        authority_node_id="node-a",
        origin_instance_id="node-a",
    )
    attachment = await store.record_node_attachment(
        node_attachment_id="att-1",
        logical_agent_id=slot.logical_agent_id,
        work_session_id=session.work_session_id,
        session_epoch=session.session_epoch,
        node_instance_id="node-b",
        authority_epoch=session.authority_epoch,
        hard_expires_at=session.hard_expires_at,
    )
    await store.record_attachment_presence(
        logical_agent_id=slot.logical_agent_id,
        work_session_id=session.work_session_id,
        session_epoch=session.session_epoch,
        node_instance_id="node-b",
        task_summary="Fleet projection",
        intent="Project current scoped intent",
        work_scope=["src/terminal_mcp/fleet"],
        details=["source", "projection"],
        current_step=1,
    )
    await store.create_message_obligation(
        message_ref="node-a:msg:1",
        logical_agent_id=slot.logical_agent_id,
        sender_agent_id="manager",
        text="reply required",
        require_reply=True,
        alert=True,
    )
    journal = EventJournalStore(runtime_path)
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta.sqlite3",
        fleet_id="fleet-a",
        node_id="node-a",
    )
    await meta.initialize()
    source = FleetSourceService(runtime_path, journal, meta)

    snapshot = await source.snapshot()
    by_type = {}
    for entity in snapshot["entities"]:
        by_type.setdefault(entity["entity_type"], []).append(entity)
    assert by_type["node_attachment"][0]["entity_id"] == attachment["node_attachment_id"]
    presence = by_type["attachment_presence"][0]
    assert presence["payload"]["work_session_id"] == session.work_session_id
    assert presence["payload"]["session_epoch"] == session.session_epoch
    assert presence["payload"]["intent"] == "Project current scoped intent"
    assert presence["payload"]["details"] == ["source", "projection"]
    obligation = by_type["message_obligation"][0]
    assert obligation["payload"]["require_reply"] is True
    assert obligation["payload"]["alert"] is True

    page = await source.events(since=0, limit=100)
    projected_types = {item["entity_type"] for item in page["events"]}
    assert "node_attachment" in projected_types
    assert "attachment_presence" in projected_types
    assert "message_obligation" in projected_types

"""Fleet source integration over real runtime, journal, node metadata and auth stores."""

import pytest

from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.fleet.protocol import canonical_event_id
from terminal_mcp.fleet.source import FleetSourceService
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


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


@pytest.mark.asyncio
async def test_source_projects_persistent_attachment_presence_and_obligation(tmp_path):
    runtime_path = tmp_path / "terminal.sqlite3"
    repo = SqliteRepository(runtime_path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(runtime_path)
    await store.create_slot("logical-1", "Agent One", "A1B2", authority_node_id="node-a")
    armed, _ = await store.arm_slot("logical-1", 120, expected_revision=1)
    slot, session = await store.start_session(
        selector="A1B2",
        work_session_id="ws-1",
        expected_revision=armed.slot_revision,
        principal_id="client-1",
        auth_generation=1,
        authority_node_id="node-a",
        origin_instance_id="node-a",
        session_duration_seconds=120,
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


@pytest.mark.asyncio
async def test_source_event_replay_enriches_logical_agent_with_authoritative_public_name(tmp_path):
    runtime_path = tmp_path / "terminal.sqlite3"
    auth_path = tmp_path / "auth.sqlite3"
    repo = SqliteRepository(runtime_path, tmp_path / "output.sqlite3")
    await repo.initialize()
    persistent = PersistentAgentStore(runtime_path)
    await persistent.create_slot(
        "logical-identity",
        "Builder",
        "A1B2",
        authority_node_id="node-a",
        now="2026-10-03T00:00:00Z",
    )
    auth = AuthFoundationStore(auth_path)
    await auth.initialize()
    registered = await auth.register_access_slot("logical-identity", "node-a")
    assert registered["public_name"] == "Alpha"

    journal = EventJournalStore(runtime_path)
    meta = FleetNodeMetaStore(
        tmp_path / "fleet-node-meta-identity.sqlite3", fleet_id="fleet-a", node_id="node-a"
    )
    await meta.initialize()
    source = FleetSourceService(runtime_path, journal, meta, auth_db_path=auth_path)

    page = await source.events(since=0, limit=100)
    event = next(item for item in page["events"] if item["entity_type"] == "logical_agent")
    assert event["payload"]["public_name"] == "Alpha"
    assert event["payload"]["access_generation"] == 0
    assert "access_code" not in event["payload"]

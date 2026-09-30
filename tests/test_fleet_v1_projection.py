import pytest

from terminal_mcp.fleet.projection_storage import FleetProjectionError, FleetProjectionStore


def snapshot(generation="gen-a", barrier=5):
    return {
        "fleet_id": "fleet-a",
        "node_id": "source-a",
        "source_stream_generation": generation,
        "barrier_source_seq": barrier,
        "complete_entity_types": ["logical_agent", "attachment_presence"],
        "entities": [
            {
                "entity_type": "logical_agent",
                "entity_id": "logical-1",
                "entity_revision": 2,
                "payload_version": 1,
                "payload": {"state": "active"},
            },
            {
                "entity_type": "attachment_presence",
                "entity_id": "att-1",
                "entity_revision": barrier,
                "payload_version": 1,
                "payload": {
                    "logical_agent_id": "logical-1",
                    "work_session_id": "ws-1",
                    "session_epoch": 1,
                    "node_instance_id": "source-b",
                    "intent": "project me",
                },
            },
        ],
    }


def page(source_seq=6, revision=3, event_id="event-6"):
    return {
        "fleet_id": "fleet-a",
        "node_id": "source-a",
        "source_stream_generation": "gen-a",
        "next_cursor": source_seq,
        "reset_required": False,
        "events": [
            {
                "event_id": event_id,
                "source_seq": source_seq,
                "event_type": "logical_agent.changed",
                "entity_type": "logical_agent",
                "entity_id": "logical-1",
                "entity_revision": revision,
                "payload_version": 1,
                "payload": {"state": "suspended"},
                "created_at": "2026-01-01T00:00:01Z",
            }
        ],
    }


@pytest.mark.asyncio
async def test_projection_snapshot_event_idempotency_and_overlay_are_separate(tmp_path):
    store = FleetProjectionStore(
        tmp_path / "projection.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-a",
        owner_node_id="projection-a",
    )
    await store.initialize()
    assert store.sqlite_diagnostics.role == "fleet_projection"

    await store.apply_snapshot(snapshot())
    after_snapshot = await store.snapshot()
    presence = next(
        item
        for item in after_snapshot["entities"]
        if item["entity_type"] == "attachment_presence"
    )
    assert presence["payload"]["work_session_id"] == "ws-1"
    seq_before = after_snapshot["projection_seq"]

    await store.apply_source_page(page())
    first = await store.snapshot()
    logical = next(
        item for item in first["entities"] if item["entity_type"] == "logical_agent"
    )
    assert logical["payload"]["state"] == "suspended"
    assert first["projection_seq"] == seq_before + 1

    await store.apply_source_page(page())
    duplicate = await store.snapshot()
    assert duplicate["projection_seq"] == first["projection_seq"]

    await store.put_runtime_overlay(
        "source-a",
        {
            "finalization_pending_commands": ["cmd-1"],
            "stale_running_commands": ["cmd-2"],
            "unowned_running_commands": ["cmd-2"],
        },
    )
    overlay = await store.snapshot()
    assert overlay["projection_seq"] == first["projection_seq"]
    assert overlay["runtime_overlays"][0]["payload"]["unowned_running_commands"] == ["cmd-2"]

    await store.clear_runtime_overlay("source-a")
    stale = await store.snapshot()
    assert stale["runtime_overlays"][0]["freshness"] == "stale"
    assert stale["projection_seq"] == first["projection_seq"]


@pytest.mark.asyncio
async def test_projection_reset_and_follower_promotion_are_explicit(tmp_path):
    owner = FleetProjectionStore(
        tmp_path / "owner.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-a",
        owner_node_id="projection-a",
    )
    await owner.initialize()
    await owner.apply_snapshot(snapshot())

    with pytest.raises(FleetProjectionError, match="reset required"):
        await owner.apply_source_page(
            {
                "fleet_id": "fleet-a",
                "node_id": "source-a",
                "source_stream_generation": "gen-b",
                "reset_required": True,
                "events": [],
            }
        )
    state = await owner.snapshot()
    assert state["sources"][0]["freshness"] == "reset_required"

    follower = FleetProjectionStore(
        tmp_path / "follower.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-b",
        owner_node_id="projection-a",
        role="follower",
    )
    await follower.initialize()
    with pytest.raises(FleetProjectionError, match="cannot independently consume"):
        await follower.apply_snapshot(snapshot())
    promoted = await follower.promote(expected_epoch=1)
    assert promoted["projection_epoch"] == 2
    assert promoted["role"] == "owner"
    assert promoted["owner_node_id"] == "projection-b"


@pytest.mark.asyncio
async def test_follower_applies_exact_owner_prefix_and_fences_old_epoch(tmp_path):
    owner = FleetProjectionStore(
        tmp_path / "owner.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-a",
        owner_node_id="projection-a",
    )
    await owner.initialize()
    await owner.apply_snapshot(snapshot())
    base = await owner.replica_snapshot()

    follower = FleetProjectionStore(
        tmp_path / "follower.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-b",
        owner_node_id="projection-a",
        role="follower",
    )
    await follower.initialize()
    replicated = await follower.apply_owner_snapshot(
        base, owner_node_id="projection-a"
    )
    assert replicated["projection_epoch"] == base["projection_epoch"]
    assert replicated["projection_seq"] == base["projection_seq"]

    await owner.apply_source_page(page())
    owner_events = await owner.events(since=base["projection_seq"])
    applied = await follower.apply_owner_events(
        owner_events, owner_node_id="projection-a"
    )
    assert applied["projection_seq"] == owner_events["projection_seq"]

    follower_state = await follower.snapshot()
    owner_state = await owner.snapshot()
    follower_logical = next(
        item
        for item in follower_state["entities"]
        if item["entity_type"] == "logical_agent"
    )
    owner_logical = next(
        item
        for item in owner_state["entities"]
        if item["entity_type"] == "logical_agent"
    )
    assert follower_logical["payload"] == owner_logical["payload"]

    with pytest.raises(FleetProjectionError, match="prefix gap"):
        await follower.apply_owner_events(
            {
                "projection_epoch": applied["projection_epoch"],
                "projection_seq": applied["projection_seq"] + 2,
                "events": [
                    {
                        **owner_events["events"][0],
                        "projection_seq": applied["projection_seq"] + 2,
                        "event_id": "gap-event",
                    }
                ],
            },
            owner_node_id="projection-a",
        )

    promoted = await follower.promote(expected_epoch=applied["projection_epoch"])
    assert promoted["projection_epoch"] == applied["projection_epoch"] + 1
    follower.role = "follower"
    follower.owner_node_id = "projection-a"
    with pytest.raises(FleetProjectionError, match="old projection epoch fenced"):
        await follower.apply_owner_events(
            owner_events,
            owner_node_id="projection-a",
        )

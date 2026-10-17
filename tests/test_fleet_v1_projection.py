"""Current materialized Fleet projection transactions, without multi-node replay scenarios."""

import pytest

from terminal_mcp.fleet.projection_storage import FleetProjectionError, FleetProjectionStore


@pytest.fixture
async def owner(tmp_path):
    store = FleetProjectionStore(
        tmp_path / "projection.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-a",
        owner_node_id="projection-a",
    )
    await store.initialize()
    await store.mark_source("source-a", "gen-a", "fresh")
    return store


@pytest.fixture
async def follower(tmp_path):
    store = FleetProjectionStore(
        tmp_path / "follower.sqlite3",
        fleet_id="fleet-a",
        node_id="projection-b",
        owner_node_id="projection-a",
        role="follower",
    )
    await store.initialize()
    return store


def delta():
    return {
        "fleet_id": "fleet-a",
        "node_id": "source-a",
        "source_stream_generation": "gen-a",
        "next_cursor": 1,
    }


def mutation():
    return {
        "event_id": "event-a",
        "source_seq": 1,
        "event_type": "projection.upsert",
        "entity_type": "logical_agent",
        "entity_id": "logical-a",
        "entity_revision": 2,
        "payload_version": 2,
        "payload": {"state": "suspended"},
    }


@pytest.mark.asyncio
async def test_materialized_delta_updates_the_visible_entity(owner):
    await owner.apply_materialized_source_page(delta(), [mutation()])

    state = await owner.snapshot()
    assert state["entities"][0]["payload"] == {"state": "suspended"}


@pytest.mark.asyncio
async def test_duplicate_materialized_event_keeps_the_projection_sequence(owner):
    await owner.apply_materialized_source_page(delta(), [mutation()])
    previous = (await owner.meta())["projection_seq"]

    await owner.apply_materialized_source_page(delta(), [mutation()])

    assert (await owner.meta())["projection_seq"] == previous


@pytest.mark.asyncio
async def test_runtime_overlay_preserves_the_durable_projection_sequence(owner):
    previous = (await owner.meta())["projection_seq"]

    await owner.put_runtime_overlay("source-a", {"finalization_pending_commands": ["cmd-a"]})

    state = await owner.snapshot()
    assert state["projection_seq"] == previous
    assert state["runtime_overlays"][0]["payload"]["finalization_pending_commands"] == ["cmd-a"]


@pytest.mark.asyncio
async def test_generation_mismatch_requires_source_recovery(owner):
    with pytest.raises(FleetProjectionError, match="generation changed"):
        await owner.apply_materialized_source_page(
            delta() | {"source_stream_generation": "gen-b"}, []
        )


@pytest.mark.asyncio
async def test_current_recovery_page_publishes_a_completed_scope(owner):
    await owner.begin_current_recovery("source-a", "gen-a", "recovery-a", ["logical_agents"])
    page = {
        **delta(),
        "scope": "logical_agents",
        "snapshot_id": "snapshot-a",
        "barrier_source_seq": 5,
        "page_complete": True,
        "entities": [
            {
                "entity_type": "logical_agent",
                "entity_id": "logical-a",
                "entity_revision": 2,
                "payload_version": 2,
                "payload": {"state": "active"},
            }
        ],
    }

    await owner.stage_current_recovery_page("source-a", "recovery-a", page)

    state = await owner.snapshot()
    assert state["entities"][0]["payload"] == {"state": "active"}


@pytest.mark.asyncio
async def test_follower_promotion_increments_the_authority_epoch(follower):
    result = await follower.promote(expected_epoch=1, control_authorized=True)

    assert (result["owner_node_id"], result["projection_epoch"], result["role"]) == (
        "projection-b",
        2,
        "owner",
    )


@pytest.mark.asyncio
async def test_projection_promotion_requires_control_authority(follower):
    with pytest.raises(FleetProjectionError, match="control authority"):
        await follower.promote(expected_epoch=1)


@pytest.mark.asyncio
async def test_follower_rejects_an_old_owner_epoch(follower):
    with pytest.raises(FleetProjectionError, match="old projection epoch fenced"):
        await follower.apply_owner_snapshot(
            {"fleet_id": "fleet-a", "projection_epoch": 0, "projection_seq": 1},
            owner_node_id="projection-a",
        )

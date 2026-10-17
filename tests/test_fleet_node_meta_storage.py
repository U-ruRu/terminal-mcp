"""Fleet node metadata owns durable stream generation, identity and observation locking."""

import pytest

from terminal_mcp.fleet.source_meta import FleetNodeMetaStore


@pytest.mark.asyncio
async def test_node_meta_rotates_generation_for_a_regressed_journal(tmp_path):
    store = FleetNodeMetaStore(tmp_path / "meta.sqlite3", fleet_id="fleet-a", node_id="node-a")
    await store.initialize()
    previous, _ = await store.observe_journal(11)

    current, rotated = await store.observe_journal(3)

    assert rotated is True
    assert current.source_stream_generation != previous.source_stream_generation
    assert current.served_high_water == 3


@pytest.mark.asyncio
async def test_node_meta_rejects_replacement_node_identity(tmp_path):
    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    await store.initialize()
    replacement = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-b")
    with pytest.raises(RuntimeError, match="does not match"):
        await replacement.initialize()

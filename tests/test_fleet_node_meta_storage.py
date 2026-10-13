"""Fleet node metadata owns durable stream generation, identity and observation locking."""

import pytest

from terminal_mcp.fleet.source_meta import FleetNodeMetaStore


@pytest.mark.asyncio
async def test_node_meta_persists_and_rotates_generation_on_runtime_regression(tmp_path):
    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    first = await store.initialize()
    assert first.served_high_water == 0
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


@pytest.mark.asyncio
async def test_node_meta_rejects_replacement_node_identity(tmp_path):
    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    await store.initialize()
    replacement = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-b")
    with pytest.raises(RuntimeError, match="does not match"):
        await replacement.initialize()


def test_node_meta_declares_its_sqlite_role(tmp_path):
    path = tmp_path / "fleet-node-meta.sqlite3"
    store = FleetNodeMetaStore(path, fleet_id="fleet-a", node_id="node-a")
    assert store.sqlite_diagnostics.role == "fleet_node_meta"


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
        item[0].source_stream_generation == advanced.source_stream_generation for item in results
    )

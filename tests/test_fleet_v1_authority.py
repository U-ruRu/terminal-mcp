import pytest

from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore


@pytest.mark.asyncio
async def test_fleet_control_rejects_wal_sidecar_bytes_at_main_path(tmp_path):
    path = tmp_path / "fleet-control.sqlite3"
    original = bytes.fromhex("377f0682") + b"\x00" * 4092
    path.write_bytes(original)
    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )

    with pytest.raises(FleetControlError, match="fleet_control_invalid_header"):
        await store.initialize()
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_fleet_control_health_uses_read_only_quick_check(tmp_path):
    path = tmp_path / "fleet-control.sqlite3"
    store = FleetControlStore(
        path,
        fleet_id="fleet-a",
        node_id="node-a",
        control_node_id="node-a",
    )
    await store.initialize()
    assert await store.healthy() is True

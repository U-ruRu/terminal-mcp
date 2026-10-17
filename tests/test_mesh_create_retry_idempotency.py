"""A lost Create Mesh reply may be retried without a duplicate or a revision bump."""

import pytest

from terminal_mcp.fleet.control_storage import FleetControlStore


@pytest.mark.asyncio
async def test_repeated_mesh_create_preserves_committed_revision(tmp_path):
    store = FleetControlStore(
        tmp_path / "mesh-idempotent.sqlite3",
        fleet_id="fleet-a",
        node_id="control-a",
        control_node_id="control-a",
    )
    await store.initialize()
    creation = dict(
        mesh_id="mesh-retry",
        display_name="Fleet",
        nodes=[{"node_id": "control-a"}],
        policy={
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": False,
        },
        attach_local_control=True,
    )
    initial = await store.adopt_managed(**creation)
    repeated = await store.adopt_managed(**creation)
    assert repeated["revisions"] == initial["revisions"]
    assert repeated["meshes"] == initial["meshes"]

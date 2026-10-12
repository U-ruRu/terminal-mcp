"""A lost Create Mesh reply may be retried without a duplicate or a revision bump."""

import pytest

from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore


@pytest.mark.asyncio
async def test_repeat_create_exact_mesh_is_idempotent_and_mismatched_name_rejected(tmp_path):
    store = FleetControlStore(
        tmp_path / "mesh-idempotent.sqlite3",
        fleet_id="fleet-a", node_id="control-a", control_node_id="control-a",
    )
    await store.initialize()
    policy = {
        "duration_seconds": 1380, "warning_after_seconds": 1200,
        "alert_after_seconds": 1320, "rearm_after_seconds": 180,
        "legacy_admission_enabled": False,
    }
    async def create(name: str, mesh_id: str = "mesh-retry"):
        return await store.adopt_managed(
            mesh_id=mesh_id, display_name=name, nodes=[{"node_id": "control-a"}],
            policy=policy, attach_local_control=True,
        )

    initial = await create("Fleet")
    repeated = await create("Fleet")
    assert repeated["revisions"] == initial["revisions"]
    assert repeated["meshes"] == initial["meshes"]
    assert len(repeated["meshes"]) == 1
    with pytest.raises(FleetControlError, match="mesh_already_exists"):
        await create("Different")
    again = await store.control_state(include_secrets=False)
    assert again["revisions"] == initial["revisions"]
    assert len(again["meshes"]) == 1

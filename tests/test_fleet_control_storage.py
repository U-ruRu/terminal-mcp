"""Current Fleet control transactions, each with a single mutation under test."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_fleet_control_hardening import PolicyProbe, keypair

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore

POLICY = {
    "duration_seconds": 1200,
    "warning_after_seconds": 900,
    "alert_after_seconds": 1100,
    "rearm_after_seconds": 120,
}


@pytest.fixture
async def control(tmp_path):
    store = FleetControlStore(
        tmp_path / "control.sqlite3", fleet_id="fleet-a", node_id="home", control_node_id="home"
    )
    await store.initialize()
    return store


@pytest.fixture
async def managed(control):
    _, public = keypair()
    await control.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[{"node_id": "home", "public_key": public}],
        policy=POLICY,
        attach_local_control=True,
    )
    return control


@pytest.mark.asyncio
async def test_route_publication_records_the_authority(control):
    result = await control.publish_route("logical-a", "home", 3)

    assert (result["authority_node_id"], result["authority_epoch"], result["state"]) == (
        "home",
        3,
        "active",
    )


@pytest.mark.asyncio
async def test_duplicate_route_keeps_its_revision(control):
    original = await control.publish_route("logical-a", "home", 3)

    result = await control.publish_route("logical-a", "home", 3)

    assert result["routing_revision"] == original["routing_revision"]


@pytest.mark.asyncio
async def test_route_publication_rejects_a_stale_authority_epoch(control):
    await control.publish_route("logical-a", "home", 3)

    with pytest.raises(FleetControlError, match="stale_authority_epoch"):
        await control.publish_route("logical-a", "old", 2)


@pytest.mark.asyncio
async def test_transfer_commit_moves_the_durable_route(control):
    await control.publish_route("logical-a", "home", 3)
    await control.prepare_transfer("logical-a", "home", "target", 3)
    await control.advance_transfer("logical-a", "imported")

    await control.advance_transfer("logical-a", "committed")

    route = await control.route("logical-a")
    assert (route["authority_node_id"], route["authority_epoch"], route["state"]) == (
        "target",
        4,
        "committed",
    )


@pytest.mark.asyncio
async def test_mesh_rename_updates_the_display_name(managed):
    result = await managed.rename_managed_mesh("mesh-a", "Renamed")

    assert result["meshes"][0]["display_name"] == "Renamed"


@pytest.mark.asyncio
async def test_detaching_the_control_node_leaves_it_standalone(managed):
    result = await managed.detach_managed_node("home")

    local = next(node for node in result["nodes"] if node["node_id"] == "home")
    assert (local["mesh_id"], local["state"]) == (None, "active")


@pytest.mark.asyncio
async def test_policy_update_keeps_topology_and_trust_revisions(managed):
    before = (await managed.control_state())["revisions"]

    await managed.update_access_policy(**(POLICY | {"duration_seconds": 1500}))

    after = (await managed.control_state())["revisions"]
    assert after == before | {"access_policy": before["access_policy"] + 1}


@pytest.mark.asyncio
async def test_identity_rotation_commits_the_prepared_key(control):
    private, _ = keypair()
    await control.ensure_managed_identity(private)
    pending = await control.prepare_managed_identity_rotation()

    identity = await control.commit_managed_identity_rotation(pending["generation"])

    assert (identity["public_key"], identity["generation"]) == (
        pending["public_key"],
        pending["generation"],
    )


@pytest.mark.asyncio
async def test_standalone_state_restores_local_policy():
    policy = PolicyProbe()
    private, _ = keypair()
    controller = ManagedFleetControl(
        SimpleNamespace(control_node_id="home"),
        FleetConfig("home", private, (), 1.0, 1.0),
        policy,
        public_base_url="https://home.example",
    )
    policy.current = POLICY.copy()

    await controller._apply_local({"managed": False})

    assert policy.current == policy.local


@pytest.mark.asyncio
async def test_replica_policy_restores_standalone_membership():
    policy = PolicyProbe()
    private, _ = keypair()
    store = SimpleNamespace(
        control_node_id="home",
        managed_peer_material=AsyncMock(return_value=[]),
        managed_node=AsyncMock(return_value=None),
        mark_managed_applied=AsyncMock(),
    )
    controller = ManagedFleetControl(
        store,
        FleetConfig("member", private, (), 1.0, 1.0),
        policy,
        public_base_url="https://member.example",
    )
    controller._ensure_local_identity = AsyncMock(return_value={"ingress_token": "token"})
    policy.current = POLICY.copy()

    await controller._apply_local(
        {
            "managed": True,
            "nodes": [{"node_id": "member", "mesh_id": None, "state": "active"}],
            "policy": POLICY,
        }
    )

    assert policy.current == policy.local


def test_committed_mutation_reports_pending_replica_convergence():
    state = {
        "revisions": {"topology": 7},
        "nodes": [
            {
                "state": "active",
                "desired_topology_revision": 7,
                "applied_topology_revision": 6,
            }
        ],
    }

    result = ManagedFleetControl.mutation_completion(state)

    assert result == {"status": "committed", "convergence": "pending", "topology_revision": 7}

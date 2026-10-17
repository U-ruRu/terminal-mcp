"""Atomic membership preparation, local cleanup and recovery boundaries."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from test_fleet_control_hardening import PolicyProbe, _managed_replica_snapshot, keypair
from test_fleet_control_storage import POLICY

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore


@pytest.fixture
async def topology(tmp_path):
    private, public = keypair()
    store = FleetControlStore(
        tmp_path / "control.sqlite3",
        fleet_id="local-home",
        node_id="home",
        control_node_id="home",
    )
    await store.initialize()
    await store.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[{"node_id": "home"}],
        policy=POLICY,
    )
    controller = ManagedFleetControl(
        store,
        FleetConfig("home", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://home.example",
    )
    controller._apply_local = AsyncMock()
    controller._transition_peer_call = AsyncMock(return_value={"ok": True})
    return store, controller, public


async def test_transition_id_replay_preserves_the_original_intent(topology):
    store, _, _ = topology
    args = dict(
        node_id="home",
        operation_id="detach-one",
        target_mesh_id=None,
        target_authority_id="home",
        coordinator_id="home",
        payload={"incoming": True},
    )
    original = await store.prepare_topology_transition(**args)

    repeated = await store.prepare_topology_transition(**args)

    assert repeated == original


async def test_transition_id_rejects_a_different_destination(topology):
    store, _, _ = topology
    args = dict(
        node_id="home",
        operation_id="move-one",
        target_mesh_id="mesh-b",
        target_authority_id="other",
        coordinator_id="home",
        payload={},
    )
    await store.prepare_topology_transition(**args)

    with pytest.raises(FleetControlError, match="mesh_operation_id_conflict"):
        await store.prepare_topology_transition(**(args | {"target_mesh_id": "mesh-c"}))


async def test_pending_local_transition_blocks_membership_before_topology_commit(topology):
    store, controller, _ = topology
    controller.native_access = AsyncMock()
    controller.native_access.replication = None
    controller.native_access.begin_topology_transition.side_effect = AccessMeshError(
        "mesh_transition_pending"
    )
    before = await store.control_state()

    with pytest.raises(FleetControlError, match="mesh_transition_pending"):
        await controller._commit_detach_node("home")

    assert (await store.control_state())["revisions"] == before["revisions"]
    assert (await store.managed_node("home"))["mesh_id"] == "mesh-a"


async def test_member_departure_preserves_the_source_registry(topology):
    store, controller, public = topology
    await store.upsert_managed_node(
        node_id="member",
        mesh_id="mesh-a",
        origin="https://member.example",
        public_key=public,
        auth_token="member-enrollment",
    )
    controller.native_access = AsyncMock()
    controller.native_access.replication = None

    await controller._commit_detach_node("member")

    controller.native_access.begin_topology_transition.assert_awaited_once()
    controller.native_access.finish_topology_transition.assert_not_awaited()


async def test_primary_departure_requires_a_successor_before_fencing(topology):
    store, controller, public = topology
    await store.upsert_managed_node(
        node_id="member",
        mesh_id="mesh-a",
        origin="https://member.example",
        public_key=public,
        auth_token="member-enrollment",
    )
    controller.native_access = AsyncMock()
    controller.native_access.replication = None

    with pytest.raises(FleetControlError, match="mesh_primary_handoff_required"):
        await controller.prepare_local_transition(
            operation_id="detach-primary",
            target_mesh_id=None,
            target_authority_id="home",
            coordinator_id="home",
        )

    controller.native_access.begin_topology_transition.assert_not_awaited()


async def test_replica_detach_finishes_locally_without_source_acknowledgement(tmp_path):
    private, _ = keypair()
    store = FleetControlStore(
        tmp_path / "member.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()
    current = _managed_replica_snapshot(member_mesh_id="mesh-a")
    await store.apply_managed_replica(current)
    detached = deepcopy(current)
    detached["nodes"][1].update(mesh_id=None, state="draining")
    detached["revisions"]["topology"] += 1
    controller = ManagedFleetControl(
        store,
        FleetConfig("member", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://member.example",
    )
    controller._forward_mutation_to = AsyncMock(side_effect=AssertionError("source RPC"))

    await controller.apply_replica(detached, source_node_id="control")

    assert store.control_node_id == "member"
    assert store.fleet_id == "local-member"
    assert (await store.control_state())["managed"] is False


async def test_recovery_finishes_the_committed_local_scope_once(topology):
    store, controller, _ = topology
    controller.native_access = AsyncMock()
    controller.native_access.replication = None
    await store.prepare_topology_transition(
        node_id="home",
        operation_id="join-a",
        target_mesh_id="mesh-a",
        target_authority_id="home",
        coordinator_id="home",
        payload={"incoming": True},
    )

    await controller._recover_membership_changes()
    await controller._recover_membership_changes()

    controller.native_access.finish_topology_transition.assert_awaited_once_with(
        "join-a",
        namespace_id="mesh-a",
        authority_id="home",
        mesh_id="mesh-a",
        clear_local=True,
    )


async def test_retired_bootstrap_peer_token_does_not_authorize_topology(topology):
    _, controller, public = topology
    controller.bootstrap_config = FleetConfig(
        "home",
        controller.config.signing_private_key,
        (FleetPeer("old", "https://old.example", public, "retired-token"),),
        1.0,
        1.0,
    )

    result = await controller.authenticate_management_peer("old", "Bearer retired-token")

    assert result is None

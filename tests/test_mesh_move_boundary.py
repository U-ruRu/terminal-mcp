"""Three-node Move keeps source authority and credential boundaries intact."""

from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from test_fleet_control_hardening import PolicyProbe, keypair
from test_fleet_control_storage import POLICY
from test_mesh_primary_handoff import RegistryProbe
from test_mesh_primary_handoff import mesh_pair as mesh_pair

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.http.fleet_control import build_fleet_control_router


class ScopeProbe(RegistryProbe):
    async def finish_topology_transition(self, operation_id, *, clear_local, **scope):
        if clear_local:
            self.records = {"namespace_id": scope["namespace_id"], "tables": {}}
        await self.abort_topology_transition(operation_id)


@pytest.fixture
async def moving_mesh(mesh_pair, tmp_path):
    source, moving, records, _ = mesh_pair
    source.native_access = ScopeProbe(records)
    moving.native_access = ScopeProbe(records)
    private, _ = keypair()
    store = FleetControlStore(
        tmp_path / "destination-control.db",
        fleet_id="mesh-b",
        node_id="destination",
        control_node_id="destination",
    )
    await store.initialize()
    destination = ManagedFleetControl(
        store,
        FleetConfig("destination", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://destination.example",
    )
    identity = await destination._ensure_local_identity()
    await store.adopt_managed(
        mesh_id="mesh-b",
        display_name="Beta",
        nodes=[
            {
                "node_id": "destination",
                "origin": destination.public_base_url,
                "public_key": identity["public_key"],
                "auth_token": identity["ingress_token"],
            }
        ],
        policy=POLICY,
    )
    destination.native_access = ScopeProbe({"namespace_id": "mesh-b", "tables": {}})
    transports = {}
    for controller in (source, moving, destination):
        controller._schedule_deferred_reconcile = Mock()
        app = FastAPI()
        app.include_router(build_fleet_control_router(controller, None))
        transports[controller.store.node_id + ".example"] = httpx.ASGITransport(app=app)

    async def dispatch(request):
        return await transports[request.url.host].handle_async_request(request)

    for controller in (source, moving, destination):
        controller.client_factory = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch)
        )
    return source, moving, destination, deepcopy(records), dispatch


async def move(destination, descriptor):
    return await destination.upsert_node(mesh_id="mesh-b", **descriptor)


async def test_direct_move_fences_source_and_refreshes_target_enrollment(moving_mesh):
    source, moving, destination, records, _ = moving_mesh
    descriptor = await moving.enrollment_descriptor()

    await move(destination, descriptor)

    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-b"
    assert source.native_access.records == records
    assert moving.native_access.records["namespace_id"] == "mesh-b"
    assert moving.native_access.records["tables"] == {}
    assert (await source.store.managed_node("target"))["state"] == "detached"
    assert all(
        controller.native_access.guard is None for controller in (source, moving, destination)
    )
    assert (await destination.store.managed_node("target"))["auth_token"] != descriptor[
        "auth_token"
    ]
    assert (
        await moving.authenticate_management_peer(
            "source",
            "Bearer " + descriptor["auth_token"],
        )
        is None
    )


async def test_lost_move_prepare_response_retries_with_bound_transition_credential(moving_mesh):
    source, moving, destination, records, dispatch = moving_mesh
    descriptor = await moving.enrollment_descriptor()
    lost = False

    async def lose_once(request):
        nonlocal lost
        response = await dispatch(request)
        if (
            not lost
            and request.url.path.endswith("transition/prepare")
            and b'"target_mesh_id":"mesh-b"' in request.content
        ):
            lost = True
            raise httpx.ReadTimeout("lost prepared move response")
        return response

    destination.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lose_once))
    with pytest.raises(httpx.ReadTimeout):
        await move(destination, descriptor)
    pending = await moving.store.topology_transition("target")
    assert (
        await moving.authenticate_management_peer(
            "destination",
            "Bearer " + descriptor["auth_token"],
            transition_operation_id="another-operation",
        )
        is None
    )

    await move(destination, descriptor)

    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-b"
    assert source.native_access.records == records
    assert (
        await moving.authenticate_management_peer(
            "destination",
            "Bearer " + descriptor["auth_token"],
            transition_operation_id=pending["operation_id"],
        )
        is None
    )


async def test_direct_move_preserves_active_source_registry_before_detach(moving_mesh):
    source, moving, destination, records, _ = moving_mesh
    source.native_access.active = True
    descriptor = await moving.enrollment_descriptor()
    original_ingress = (await moving.store.managed_identity())["ingress_token"]

    await move(destination, descriptor)

    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-b"
    assert source.native_access.records == records
    assert (await source.store.managed_node("target"))["mesh_id"] is None
    assert (await moving.store.managed_identity())["ingress_token"] != original_ingress
    assert destination.native_access.guard is None


async def test_active_standalone_joins_running_mesh_without_copying_all_sessions(moving_mesh):
    _, moving, destination, _, _ = moving_mesh
    await moving.detach_node("target")
    moving.native_access.active = True
    destination.native_access.active = True
    descriptor = await moving.enrollment_descriptor()

    await move(destination, descriptor)
    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-b"
    assert moving.native_access.records == {"namespace_id": "mesh-b", "tables": {}}
    assert moving.native_access.imports == 0
    assert destination.native_access.guard is None


async def test_prepared_move_retries_after_persisting_the_new_ingress(moving_mesh, monkeypatch):
    _, moving, destination, _, _ = moving_mesh
    descriptor = await moving.enrollment_descriptor()
    commit = destination.store.upsert_managed_node

    async def interrupted_commit(**_):
        raise httpx.ReadTimeout("interrupted after prepared ingress persisted")

    monkeypatch.setattr(destination.store, "upsert_managed_node", interrupted_commit)
    with pytest.raises(httpx.ReadTimeout):
        await move(destination, descriptor)
    pending = await destination.store.topology_transition("target")
    assert pending["payload"]["peer"]["auth_token"] != descriptor["auth_token"]

    monkeypatch.setattr(destination.store, "upsert_managed_node", commit)
    await move(destination, descriptor)

    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-b"
    assert destination.native_access.guard is None
    assert moving.native_access.guard is None


async def test_mesh_peer_ingress_does_not_authorize_an_app_topology_assignment(moving_mesh):
    _, moving, _, _, _ = moving_mesh
    ingress = (await moving.store.managed_identity())["ingress_token"]

    assert (
        await moving.authenticate_management_peer(
            "unassigned-primary",
            "Bearer " + ingress,
            first_apply_control_node_id="unassigned-primary",
        )
        is None
    )

    descriptor = await moving.enrollment_descriptor()
    assert descriptor["auth_token"] != ingress
    assert (
        await moving.authenticate_management_peer(
            "unassigned-primary",
            "Bearer " + descriptor["auth_token"],
            first_apply_control_node_id="unassigned-primary",
        )
        == "unassigned-primary"
    )


async def test_follower_create_detaches_from_active_source_mesh(moving_mesh):
    source, moving, _, _, _ = moving_mesh
    source.native_access.active = True

    await moving.adopt(mesh_id="mesh-new", control_node_id="target")

    assert (await moving.snapshot())["mesh"]["mesh_id"] == "mesh-new"
    assert (await source.store.managed_node("target"))["mesh_id"] is None


async def test_delete_mesh_detaches_members_with_active_work(moving_mesh):
    source, moving, _, _, _ = moving_mesh
    source.native_access.active = True

    await source.delete_mesh(mesh_id="mesh-a")

    assert not (await moving.snapshot())["managed"]
    assert not (await source.snapshot())["managed"]


async def test_primary_router_transfers_a_registry_larger_than_public_read_budget(moving_mesh):
    source, target, _, _, _ = moving_mesh
    source.native_access.records["tables"]["registry_history"] = [{"retained": "x" * 70_000}]
    before = deepcopy(source.native_access.records)
    source._primary_peer_call = ManagedFleetControl._primary_peer_call.__get__(source)

    await source.change_primary(node_id="target", operation_id="large-registry-handoff")

    assert source.store.control_node_id == target.store.control_node_id == "target"
    assert target.native_access.records == source.native_access.records == before

"""Minimal real-store checks across the control/ACS transaction boundary."""

import json
from dataclasses import asdict

from test_fleet_control_hardening import PolicyProbe, keypair
from test_fleet_control_storage import POLICY
from test_mesh_primary_handoff import mesh_pair as mesh_pair

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.storage.access_mesh import AccessMeshStore


def native_access(path, node_id):
    store = AccessMeshStore(
        path,
        local_node_id=node_id,
        trusted_issuers=frozenset({"source", "target", node_id}),
        proof_key=("test-proof-material-for-" + node_id).encode().ljust(40, b"0"),
    )
    return AccessMeshApplication(
        store,
        task_store=None,
        execution_fence=None,
        defaults=SlotPolicy(1200, 120),
    )


def seed_inactive_slot(store, issuer_id, slot_id):
    with store._connect() as db:
        db.execute(
            "INSERT INTO access_mesh_slot_replicas "
            "(issuer_id,slot_id,logical_agent_id,kind,state,revision,event_id,"
            "policy_json,code_tag) "
            "VALUES(?,?,?,'persistent','suspended',7,?,?,?)",
            (
                issuer_id,
                slot_id,
                "agent-" + slot_id,
                "event-" + slot_id,
                json.dumps(asdict(SlotPolicy(1200, 120))),
                store.code_tag(issuer_id, "1234"),
            ),
        )


async def test_restart_completes_namespace_purge_before_managed_policy(tmp_path):
    private, _ = keypair()
    path = tmp_path / "control.db"
    store = FleetControlStore(path, fleet_id="mesh-x", node_id="moving", control_node_id="moving")
    await store.initialize()
    await store.adopt_managed(
        mesh_id="mesh-x",
        display_name="X",
        nodes=[{"node_id": "moving"}],
        policy=POLICY,
    )
    access = native_access(tmp_path / "access.db", "moving")
    access.store.install_registry_scope(mesh_id="mesh-x", authority_id="moving")
    seed_inactive_slot(access.store, "moving", "old-slot")
    await store.prepare_topology_transition(
        node_id="moving",
        operation_id="move-y",
        target_mesh_id="mesh-y",
        target_authority_id="moving",
        coordinator_id="moving",
        payload={"incoming": True},
    )
    await access.begin_topology_transition("move-y")
    await store.adopt_managed(
        mesh_id="mesh-y",
        display_name="Y",
        nodes=[{"node_id": "moving"}],
        policy=POLICY,
    )
    # The process is lost after the topology transaction, before ACS scope commit.
    reopened = FleetControlStore(
        path, fleet_id="mesh-y", node_id="moving", control_node_id="moving"
    )
    await reopened.initialize()
    controller = ManagedFleetControl(
        reopened,
        FleetConfig("moving", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://moving.example",
    )
    controller.native_access = native_access(tmp_path / "access.db", "moving")

    await controller.reconcile_local()

    restored = controller.native_access.store
    assert restored.namespace_id == "mesh-y"
    assert restored.registry_slot("old-slot") is None
    restored.assert_registry_ready()


async def test_real_primary_handoff_preserves_slots_and_their_policy(mesh_pair, tmp_path):
    source, target, _, _ = mesh_pair
    for controller in (source, target):
        controller.native_access = native_access(
            tmp_path / f"{controller.store.node_id}-access.db",
            controller.store.node_id,
        )
        controller.native_access.store.install_registry_scope(
            mesh_id="mesh-a", authority_id="source"
        )
        await controller._apply_local(await controller.snapshot())
    seed_inactive_slot(source.native_access.store, "source", "from-source")
    seed_inactive_slot(source.native_access.store, "target", "from-target")
    before = {
        name: source.native_access.store.registry_slot(name)
        for name in ("from-source", "from-target")
    }

    await source.change_primary(node_id="target", operation_id="real-handoff")

    for controller in (source, target):
        assert {
            name: controller.native_access.store.registry_slot(name) for name in before
        } == before
        assert controller.native_access.store.registry_authority_id == "target"
        controller.native_access.store.assert_registry_ready()


async def test_hot_control_scope_conversion_preserves_existing_acs_registry(tmp_path):
    private, _ = keypair()
    control = FleetControlStore(
        tmp_path / "old-control.db",
        fleet_id="old-domain",
        node_id="source",
        control_node_id="source",
    )
    await control.initialize()
    await control.adopt_managed(
        mesh_id="mesh-a",
        display_name="A",
        nodes=[{"node_id": "source"}],
        policy=POLICY,
    )
    async with control._connect("test_previous_namespace") as db:
        await db.execute("UPDATE control_meta SET fleet_id='old-domain'")
        await db.execute("UPDATE control_schema SET version=4")
        await db.commit()
    control.fleet_id = "old-domain"
    access = native_access(tmp_path / "preserved-access.db", "source")
    access.store.install_registry_scope(mesh_id="mesh-a", authority_id="source")
    seed_inactive_slot(access.store, "source", "pre-upgrade")
    before = access.store.registry_slot("pre-upgrade")

    await control.initialize()
    controller = ManagedFleetControl(
        control,
        FleetConfig("source", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://source.example",
    )
    controller.native_access = access
    await controller.reconcile_local()

    assert control.fleet_id == "old-domain"
    assert access.store.namespace_id == "mesh-a"
    assert access.store.registry_slot("pre-upgrade") == before

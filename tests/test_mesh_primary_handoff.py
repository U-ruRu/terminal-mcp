"""Registry handoff transaction and its two-node transport failure boundary."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_fleet_control_hardening import PolicyProbe, keypair
from test_fleet_control_storage import POLICY

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore
from terminal_mcp.fleet.primary_handoff import verify_handoff_certificate


class RegistryProbe:
    def __init__(self, records):
        self.records = deepcopy(records)
        self.guard = None
        self.imports = 0
        self.active = False
        self.replication = None
        self.managed_trust_enabled = False
        self.store = SimpleNamespace(export_registry=self.export, import_registry=self.import_)

    def export(self):
        return deepcopy(self.records)

    def import_(self, records, *, expected_namespace_id):
        assert records["namespace_id"] == expected_namespace_id
        assert self.guard is not None
        self.records = deepcopy(records)
        self.imports += 1

    async def begin_topology_transition(self, operation_id):
        if self.guard is not None and self.guard != operation_id:
            raise AccessMeshError("mesh_transition_pending")
        self.guard = operation_id

    async def abort_topology_transition(self, operation_id):
        if self.guard == operation_id:
            self.guard = None

    async def finish_topology_transition(self, operation_id, *, clear_local, **scope):
        assert clear_local is False
        await self.abort_topology_transition(operation_id)

    async def apply_fleet_policy(self, state=None):
        pass


@pytest.fixture
async def mesh_pair(tmp_path):
    controllers = {}
    identities = {}
    for node_id in ("source", "target"):
        private, _ = keypair()
        store = FleetControlStore(
            tmp_path / f"{node_id}.sqlite3",
            fleet_id="mesh-a",
            node_id=node_id,
            control_node_id="source",
        )
        await store.initialize()
        controllers[node_id] = ManagedFleetControl(
            store,
            FleetConfig(node_id, private, (), 1.0, 1.0),
            PolicyProbe(),
            public_base_url=f"https://{node_id}.example",
        )
        identities[node_id] = await controllers[node_id]._ensure_local_identity()
        controllers[node_id]._schedule_deferred_reconcile = Mock()
    source, target = controllers.values()
    await source.store.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[
            {
                "node_id": "source",
                "public_key": identities["source"]["public_key"],
                "origin": "https://source.example",
                "auth_token": identities["source"]["ingress_token"],
            }
        ],
        policy=POLICY,
    )
    await source.store.upsert_managed_node(
        node_id="target",
        mesh_id="mesh-a",
        origin="https://target.example",
        public_key=identities["target"]["public_key"],
        auth_token=identities["target"]["ingress_token"],
    )
    snapshot = await source._internal_replication_snapshot(await source.snapshot())
    await target.store.apply_managed_replica(
        snapshot,
        bootstrap_tokens={
            node_id: identity["ingress_token"] for node_id, identity in identities.items()
        },
    )
    records = {
        "namespace_id": "mesh-a",
        "tables": {
            "slots": [
                {"issuer_id": "source", "slot_id": "ended-one", "revision": 7},
                {"issuer_id": "target", "slot_id": "ended-two", "revision": 3},
            ],
            "defaults": {"revision": 4, "duration_seconds": 1200},
        },
        "verification_keys": {"historical": "opaque-proof-material"},
    }
    source.native_access = RegistryProbe(records)
    target.native_access = RegistryProbe({"namespace_id": "mesh-a", "tables": {}})

    async def transport(peer, action, payload):
        if action == "prepare":
            return await target.prepare_primary_import(**payload, source_node_id="source")
        if action == "activate":
            return await target.activate_primary_import(**payload, source_node_id="source")
        return await target.abort_primary_import(**payload, source_node_id="source")

    source._primary_peer_call = transport
    return source, target, records, transport


async def test_primary_handoff_preserves_every_registry_record(mesh_pair):
    source, target, records, _ = mesh_pair

    await source.change_primary(node_id="target", operation_id="handoff-one")

    assert source.store.control_node_id == target.store.control_node_id == "target"
    assert source.native_access.records == target.native_access.records == records


async def test_lost_activation_reply_replays_before_revision_cas(mesh_pair):
    source, target, records, transport = mesh_pair
    lost = False

    async def lose_activation_once(peer, action, payload):
        nonlocal lost
        result = await transport(peer, action, payload)
        if action == "activate" and not lost:
            lost = True
            raise httpx.ReadTimeout("lost activation reply")
        return result

    source._primary_peer_call = lose_activation_once
    with pytest.raises(httpx.ReadTimeout):
        await source.change_primary(node_id="target", operation_id="handoff-retry")

    await source.change_primary(
        node_id="target",
        operation_id="handoff-retry",
        expected_topology_revision=0,
    )

    assert target.native_access.imports == 1
    assert target.native_access.records == records
    assert (await source.store.primary_handoff("handoff-retry"))["phase"] == "completed"


async def test_primary_replay_rejects_another_destination(mesh_pair):
    source, _, _, _ = mesh_pair
    await source.change_primary(node_id="target", operation_id="handoff-one")

    with pytest.raises(FleetControlError, match="mesh_operation_id_conflict"):
        await source.change_primary(node_id="source", operation_id="handoff-one")


async def test_primary_handoff_preserves_registry_with_active_work(mesh_pair):
    source, target, records, _ = mesh_pair
    source.native_access.active = True

    await source.change_primary(node_id="target", operation_id="handoff-busy")

    assert source.store.control_node_id == target.store.control_node_id == "target"
    assert source.native_access.records == target.native_access.records == records
    assert target.native_access.imports == 1


async def test_handoff_certificate_rejects_a_changed_destination(mesh_pair):
    source, _, _, _ = mesh_pair
    await source.change_primary(node_id="target", operation_id="handoff-one")
    handoff = await source.store.primary_handoff("handoff-one")
    certificate = handoff["certificate"]
    certificate["payload"]["to_node_id"] = "outsider"
    identity = await source._ensure_local_identity()

    with pytest.raises(FleetControlError, match="mesh_primary_certificate_invalid"):
        verify_handoff_certificate(certificate, identity["public_key"])


async def test_primary_prepare_does_not_clear_an_observer_registry(mesh_pair):
    source, _, records, _ = mesh_pair
    source.native_access.records = deepcopy(records)
    await source.prepare_local_transition(
        operation_id="observer-guard",
        target_mesh_id="mesh-a",
        target_authority_id="target",
        coordinator_id="source",
        preserve_registry=True,
    )

    await source._finish_local_membership(await source.snapshot())

    assert source.native_access.guard == "observer-guard"
    assert source.native_access.records == records


async def test_old_primary_certificate_cannot_reclaim_a_later_authority(mesh_pair):
    source, target, _, _ = mesh_pair
    await source.change_primary(node_id="target", operation_id="old-delegation")
    old_snapshot = await target._internal_replication_snapshot(await target.snapshot())
    # An acknowledged later handoff has returned authority to the original node.
    # A former primary still possesses its old signed delegation and peer token.
    async with source.store._connect("test_later_authority") as db:
        await db.execute("UPDATE control_meta SET control_node_id='source',topology_revision=100")
        await db.commit()
    source.store.control_node_id = "source"
    old_snapshot["revisions"]["topology"] = 101

    with pytest.raises(FleetControlError, match="mesh_primary_certificate_stale"):
        await source._verified_primary_change(old_snapshot)


async def test_primary_import_rejects_a_stale_target_key(mesh_pair):
    source, target, _, transport = mesh_pair
    private, _ = keypair()

    async def rotate_before_prepare(peer, action, payload):
        if action == "prepare":
            async with target.store._connect("test_rotated_target") as db:
                await db.execute(
                    "UPDATE managed_identity SET active_private_key=?",
                    (private,),
                )
                await db.commit()
        return await transport(peer, action, payload)

    source._primary_peer_call = rotate_before_prepare
    with pytest.raises(FleetControlError, match="mesh_primary_target_key_changed"):
        await source.change_primary(node_id="target", operation_id="stale-target-key")

    assert target.native_access.imports == 0
    assert source.store.control_node_id == "source"


async def test_startup_keeps_primary_guard_until_unavailable_target_activates(mesh_pair):
    source, _, _, transport = mesh_pair

    async def unavailable_activation(peer, action, payload):
        if action == "activate":
            raise httpx.ConnectError("target is restarting")
        return await transport(peer, action, payload)

    source._primary_peer_call = unavailable_activation
    with pytest.raises(httpx.ConnectError):
        await source.change_primary(node_id="target", operation_id="restart-target")

    await source.reconcile_local()

    assert source.native_access.guard == "restart-target"
    assert (await source.store.primary_handoff("restart-target"))["phase"] == "committed"

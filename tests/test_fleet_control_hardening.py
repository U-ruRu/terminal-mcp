from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore


def keypair():
    key = Ed25519PrivateKey.generate()
    private = base64.urlsafe_b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).rstrip(b"=").decode()
    public = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).rstrip(b"=").decode()
    return private, public


class PolicyProbe:
    def __init__(self):
        self.local = {
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": False,
        }
        self.current = dict(self.local)
        self.applied: list[dict] = []
        self.restored = 0

    def snapshot(self):
        return dict(self.current)

    async def apply_managed(self, **policy):
        self.current = dict(policy)
        self.applied.append(dict(policy))
        return dict(self.current)

    async def restore_local(self):
        self.current = dict(self.local)
        self.restored += 1
        return dict(self.current)


def _managed_replica_snapshot(*, include_member_mesh_id: bool, member_mesh_id=None) -> dict:
    member = {
        "node_id": "member",
        "origin": "https://member.example",
        "public_key": "pub-member",
        "state": "active",
        "applied_topology_revision": 2,
        "applied_trust_revision": 2,
        "applied_policy_revision": 2,
    }
    if include_member_mesh_id:
        member["mesh_id"] = member_mesh_id
    return {
        "managed": True,
        "fleet_id": "fleet-a",
        "control_node_id": "control",
        "mesh": {
            "mesh_id": "mesh-a",
            "display_name": "Alpha",
            "adopted": True,
        },
        "meshes": [{"mesh_id": "mesh-a", "display_name": "Alpha", "adopted": True}],
        "nodes": [
            {
                "node_id": "control",
                "origin": "https://control.example",
                "public_key": "pub-control",
                "mesh_id": "mesh-a",
                "state": "active",
                "applied_topology_revision": 2,
                "applied_trust_revision": 2,
                "applied_policy_revision": 2,
            },
            member,
        ],
        "policy": {
            "duration_seconds": 1380,
            "warning_after_seconds": 1200,
            "alert_after_seconds": 1320,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": True,
        },
        "revisions": {"topology": 2, "trust": 2, "access_policy": 2},
    }


@pytest.mark.asyncio
async def test_replica_explicit_null_mesh_id_overrides_legacy_mesh_fallback(tmp_path):
    store = FleetControlStore(
        tmp_path / "explicit-null.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()

    state = await store.apply_managed_replica(
        _managed_replica_snapshot(include_member_mesh_id=True, member_mesh_id=None)
    )

    member = next(node for node in state["nodes"] if node["node_id"] == "member")
    control = next(node for node in state["nodes"] if node["node_id"] == "control")
    assert member["mesh_id"] is None
    assert control["mesh_id"] == "mesh-a"


@pytest.mark.asyncio
async def test_replica_refreshes_existing_node_auth_token_from_authoritative_material(tmp_path):
    store = FleetControlStore(
        tmp_path / "refresh-token.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()
    snapshot = _managed_replica_snapshot(
        include_member_mesh_id=True,
        member_mesh_id="mesh-a",
    )

    await store.apply_managed_replica(
        snapshot,
        bootstrap_tokens={"member": "stale-token", "control": "control-token"},
    )
    before = await store.managed_node("member")
    assert before is not None
    assert before["auth_token"] == "stale-token"

    await store.apply_managed_replica(
        snapshot,
        bootstrap_tokens={"member": "rotated-token", "control": "control-token"},
    )
    after = await store.managed_node("member")
    assert after is not None
    assert after["auth_token"] == "rotated-token"


@pytest.mark.asyncio
async def test_replica_missing_mesh_id_keeps_legacy_mesh_fallback(tmp_path):
    store = FleetControlStore(
        tmp_path / "missing-mesh-id.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()

    state = await store.apply_managed_replica(
        _managed_replica_snapshot(include_member_mesh_id=False)
    )

    member = next(node for node in state["nodes"] if node["node_id"] == "member")
    assert member["mesh_id"] == "mesh-a"


@pytest.mark.asyncio
async def test_detached_local_member_restores_standalone_policy(tmp_path):
    store = FleetControlStore(
        tmp_path / "member.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()
    policy = PolicyProbe()
    member_private, _ = keypair()
    control = ManagedFleetControl(
        store,
        FleetConfig("member", member_private, (), 1.0, 1.0),
        policy,
        public_base_url="https://member.example",
    )
    managed_policy = {
        "duration_seconds": 1200,
        "warning_after_seconds": 900,
        "alert_after_seconds": 1100,
        "rearm_after_seconds": 120,
        "legacy_admission_enabled": True,
    }
    snapshot = {
        "managed": True,
        "fleet_id": "fleet-a",
        "control_node_id": "control",
        "mesh": {"mesh_id": "mesh-a", "display_name": "Alpha", "active": True},
        "meshes": [{"mesh_id": "mesh-a", "display_name": "Alpha", "active": True}],
        "nodes": [
            {"node_id": "control", "origin": "https://control.example", "public_key": "pub-control", "mesh_id": "mesh-a", "state": "active", "applied_topology_revision": 2, "applied_trust_revision": 2, "applied_policy_revision": 2},
            {"node_id": "member", "origin": "https://member.example", "public_key": "pub-member", "mesh_id": "mesh-a", "state": "active", "applied_topology_revision": 2, "applied_trust_revision": 2, "applied_policy_revision": 2},
        ],
        "policy": managed_policy,
        "revisions": {"topology": 2, "trust": 2, "access_policy": 2},
        "_peer_material": [{"node_id": "control", "auth_token": "token-control"}],
    }
    await control.apply_replica(snapshot, source_node_id="control")
    assert policy.current == managed_policy

    detached = dict(snapshot)
    detached["mesh"] = None
    detached["nodes"] = [
        dict(snapshot["nodes"][0]),
        {**snapshot["nodes"][1], "mesh_id": None},
    ]
    detached["revisions"] = {"topology": 3, "trust": 3, "access_policy": 2}
    await control.apply_replica(detached, source_node_id="control")

    assert policy.current == policy.local
    assert policy.restored >= 1
    state = await control.snapshot()
    local = next(node for node in state["nodes"] if node["node_id"] == "member")
    assert local["mesh_id"] is None


@pytest.mark.asyncio
async def test_deleted_mesh_replica_restores_standalone_policy_without_losing_pairing_state(tmp_path):
    store = FleetControlStore(
        tmp_path / "delete.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()
    policy = PolicyProbe()
    member_private, _ = keypair()
    control = ManagedFleetControl(
        store,
        FleetConfig("member", member_private, (), 1.0, 1.0),
        policy,
        public_base_url="https://member.example",
    )
    managed_policy = {
        "duration_seconds": 1200,
        "warning_after_seconds": 900,
        "alert_after_seconds": 1100,
        "rearm_after_seconds": 120,
        "legacy_admission_enabled": True,
    }
    base_nodes = [
        {"node_id": "control", "origin": "https://control.example", "public_key": "pub-control", "mesh_id": "mesh-a", "state": "active", "applied_topology_revision": 2, "applied_trust_revision": 2, "applied_policy_revision": 2},
        {"node_id": "member", "origin": "https://member.example", "public_key": "pub-member", "mesh_id": "mesh-a", "state": "active", "applied_topology_revision": 2, "applied_trust_revision": 2, "applied_policy_revision": 2},
    ]
    managed = {
        "managed": True,
        "fleet_id": "fleet-a",
        "control_node_id": "control",
        "mesh": {"mesh_id": "mesh-a", "display_name": "Alpha", "active": True},
        "meshes": [{"mesh_id": "mesh-a", "display_name": "Alpha", "active": True}],
        "nodes": base_nodes,
        "policy": managed_policy,
        "revisions": {"topology": 2, "trust": 2, "access_policy": 2},
        "_peer_material": [{"node_id": "control", "auth_token": "token-control"}],
    }
    await control.apply_replica(managed, source_node_id="control")
    assert policy.current == managed_policy

    deleted = {
        **managed,
        "mesh": None,
        "meshes": [],
        "nodes": [
            {**base_nodes[0], "mesh_id": None},
            {**base_nodes[1], "mesh_id": None},
        ],
        "revisions": {"topology": 3, "trust": 3, "access_policy": 2},
    }
    await control.apply_replica(deleted, source_node_id="control")

    assert policy.current == policy.local
    assert policy.restored >= 1
    state = await control.snapshot()
    local = next(node for node in state["nodes"] if node["node_id"] == "member")
    assert local["mesh_id"] is None
    assert local["state"] == "active"

class ControlResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class ControlClient:
    def __init__(self, *, post_handler=None, get_handler=None):
        self.post_handler = post_handler
        self.get_handler = get_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        if self.post_handler is None:
            raise AssertionError(f"unexpected POST {url}")
        return await self.post_handler(url, headers, json)

    async def get(self, url, *, headers):
        if self.get_handler is None:
            raise AssertionError(f"unexpected GET {url}")
        return await self.get_handler(url, headers)


@pytest.mark.asyncio
async def test_replica_pull_recovers_missed_detach_and_old_topology_cannot_roll_back(tmp_path):
    home_private, home_public = keypair()
    member_private, member_public = keypair()
    home_config = FleetConfig(
        "home",
        home_private,
        (FleetPeer("member", "https://member.example", member_public, "member-token"),),
        1.0,
        1.0,
    )
    member_config = FleetConfig(
        "member",
        member_private,
        (FleetPeer("home", "https://home.example", home_public, "home-token"),),
        1.0,
        1.0,
    )
    home_store = FleetControlStore(
        tmp_path / "home-convergence.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    member_store = FleetControlStore(
        tmp_path / "member-convergence.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="home",
    )
    await home_store.initialize()
    await member_store.initialize()
    home_policy = PolicyProbe()
    member_policy = PolicyProbe()
    member = ManagedFleetControl(
        member_store,
        member_config,
        member_policy,
        public_base_url="https://member.example",
    )
    delivery_mode = "apply"
    attached_snapshot = None

    async def push(url, headers, body):
        assert url.endswith("/internal/fleet/control/apply")
        if delivery_mode == "stale_ack":
            return ControlResponse({"ok": True, "control": attached_snapshot})
        applied = await member.apply_replica(body["state"], source_node_id="home")
        return ControlResponse({"ok": True, "control": applied})

    home = ManagedFleetControl(
        home_store,
        home_config,
        home_policy,
        public_base_url="https://home.example",
        client_factory=lambda: ControlClient(post_handler=push),
    )
    adopted = await home.adopt(mesh_id="mesh-a", display_name="Production")
    attached = await home.upsert_node(
        node_id="member",
        mesh_id="mesh-a",
        origin="https://member.example",
        public_key=member_public,
        auth_token="member-token",
        expected_topology_revision=adopted["revisions"]["topology"],
    )
    attached_snapshot = await home.authoritative_replication_snapshot()
    member_before = await member.snapshot()
    assert member_before["mesh"]["mesh_id"] == "mesh-a"

    delivery_mode = "stale_ack"
    detached = await home.detach_node(
        "member",
        expected_topology_revision=attached["revisions"]["topology"],
    )
    authority_member = next(node for node in detached["nodes"] if node["node_id"] == "member")
    assert authority_member["mesh_id"] is None
    assert authority_member["desired_topology_revision"] > authority_member["applied_topology_revision"]
    assert authority_member["last_error"] == "reconcile_failed:RuntimeError"
    assert (await member.snapshot())["mesh"]["mesh_id"] == "mesh-a"

    async def pull(url, headers):
        assert url.endswith("/internal/fleet/control/state")
        return ControlResponse(
            {"ok": True, "control": await home.authoritative_replication_snapshot()}
        )

    member.client_factory = lambda: ControlClient(get_handler=pull)
    recovered = await member.reconcile_pending()
    local_member = next(node for node in recovered["nodes"] if node["node_id"] == "member")
    assert recovered["mesh"] is None
    assert local_member["mesh_id"] is None
    assert recovered["revisions"]["topology"] == detached["revisions"]["topology"]
    assert member_policy.current == member_policy.local

    with pytest.raises(FleetControlError, match="managed_snapshot_stale"):
        await member.apply_replica(attached_snapshot, source_node_id="home")
    assert (await member.snapshot())["mesh"] is None

    delivery_mode = "apply"
    converged = await home.reconcile_pending()
    authority_member = next(node for node in converged["nodes"] if node["node_id"] == "member")
    assert authority_member["last_error"] is None
    assert authority_member["desired_topology_revision"] == authority_member["applied_topology_revision"]

    rejoined = await home.upsert_node(
        node_id="member",
        mesh_id="mesh-a",
        origin=None,
        public_key=None,
        auth_token=None,
        expected_topology_revision=converged["revisions"]["topology"],
    )
    assert (await member.snapshot())["mesh"]["mesh_id"] == "mesh-a"
    authority_member = next(node for node in rejoined["nodes"] if node["node_id"] == "member")
    assert authority_member["mesh_id"] == "mesh-a"
    assert authority_member["desired_topology_revision"] == authority_member["applied_topology_revision"]


@pytest.mark.asyncio
async def test_standalone_self_adopt_releases_old_authority_and_owns_new_mesh(tmp_path):
    home_store = FleetControlStore(
        tmp_path / "home-release.sqlite3",
        fleet_id="fleet-a",
        node_id="home",
        control_node_id="home",
    )
    member_store = FleetControlStore(
        tmp_path / "member-release.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="home",
    )
    await home_store.initialize()
    await member_store.initialize()

    home_private, home_public = keypair()
    member_private, member_public = keypair()
    policy = {
        "duration_seconds": 1380,
        "warning_after_seconds": 1200,
        "alert_after_seconds": 1320,
        "rearm_after_seconds": 180,
        "legacy_admission_enabled": True,
    }

    await home_store.adopt_managed(
        mesh_id="mesh-prod",
        display_name="Production",
        nodes=[{"node_id": "home"}],
        policy=policy,
    )
    state = await home_store.upsert_managed_node(
        node_id="home",
        mesh_id="mesh-prod",
        origin="https://home.example",
        public_key=home_public,
        auth_token="home-token",
        expected_topology_revision=1,
    )
    state = await home_store.upsert_managed_node(
        node_id="member",
        mesh_id="mesh-prod",
        origin="https://member.example",
        public_key=member_public,
        auth_token="member-token",
        expected_topology_revision=state["revisions"]["topology"],
    )
    detached = await home_store.detach_managed_node(
        "member",
        expected_topology_revision=state["revisions"]["topology"],
    )

    home = ManagedFleetControl(
        home_store,
        FleetConfig("home", home_private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://home.example",
    )
    member = ManagedFleetControl(
        member_store,
        FleetConfig(
            "member",
            member_private,
            (
                FleetPeer(
                    "home",
                    "https://home.example",
                    home_public,
                    "home-token",
                ),
            ),
            1.0,
            1.0,
        ),
        PolicyProbe(),
        public_base_url="https://member.example",
    )

    initial = await home.authoritative_replication_snapshot()
    await member.apply_replica(initial, source_node_id="home")
    local_before = next(
        node for node in (await member.snapshot())["nodes"] if node["node_id"] == "member"
    )
    assert local_before["mesh_id"] is None
    assert local_before["state"] == "active"

    async def forward_release(url, headers, body):
        assert url.endswith("/internal/fleet/control/mutate/release-node")
        released = await home.execute_forwarded(
            "release-node",
            body["payload"],
            authenticated_peer_id="member",
        )
        return ControlResponse({"ok": True, "control": released})

    member.client_factory = lambda: ControlClient(post_handler=forward_release)
    adopted = await member.adopt(
        mesh_id="mesh-new",
        display_name="New Mesh",
        control_node_id="member",
    )

    assert adopted["control_node_id"] == "member"
    assert adopted["mesh"]["mesh_id"] == "mesh-new"
    local_after = next(node for node in adopted["nodes"] if node["node_id"] == "member")
    assert local_after["mesh_id"] == "mesh-new"
    assert local_after["state"] == "active"

    old_view = await home_store.managed_node("member")
    assert old_view is not None
    assert old_view["state"] == "detached"
    assert old_view["mesh_id"] is None
    assert all(
        item["node_id"] != "member"
        for item in await home_store.managed_peer_material()
    )

    with pytest.raises(FleetControlError, match="control_node_mismatch"):
        await member.apply_replica(initial, source_node_id="home")

    after_stale = await member.snapshot()
    assert after_stale["control_node_id"] == "member"
    assert after_stale["mesh"]["mesh_id"] == "mesh-new"

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlStore


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

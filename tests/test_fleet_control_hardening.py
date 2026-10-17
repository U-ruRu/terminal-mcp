from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore


def keypair():
    key = Ed25519PrivateKey.generate()
    private = (
        base64.urlsafe_b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        .rstrip(b"=")
        .decode()
    )
    public = (
        base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )
        .rstrip(b"=")
        .decode()
    )
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

    async def update(self, **policy):
        self.current = dict(policy)
        self.local = dict(policy)
        return dict(self.current)


def _managed_replica_snapshot(*, member_mesh_id=None) -> dict:
    member = {
        "node_id": "member",
        "origin": "https://member.example",
        "public_key": "pub-member",
        "state": "active",
        "applied_topology_revision": 2,
        "applied_trust_revision": 2,
        "applied_policy_revision": 2,
    }
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
            "legacy_admission_enabled": False,
        },
        "revisions": {"topology": 2, "trust": 2, "access_policy": 2},
    }


@pytest.mark.asyncio
async def test_replica_preserves_explicit_standalone_membership(tmp_path):
    store = FleetControlStore(
        tmp_path / "explicit-null.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()

    state = await store.apply_managed_replica(_managed_replica_snapshot(member_mesh_id=None))

    member = next(node for node in state["nodes"] if node["node_id"] == "member")
    assert member["mesh_id"] is None


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
        member_mesh_id="mesh-a",
    )

    await store.apply_managed_replica(
        snapshot,
        bootstrap_tokens={"member": "stale-token", "control": "control-token"},
    )

    await store.apply_managed_replica(
        snapshot,
        bootstrap_tokens={"member": "rotated-token", "control": "control-token"},
    )
    after = await store.managed_node("member")
    assert after is not None
    assert after["auth_token"] == "rotated-token"


@pytest.mark.asyncio
async def test_replica_rejects_a_lower_topology_revision(tmp_path):
    store = FleetControlStore(
        tmp_path / "replica.sqlite3",
        fleet_id="fleet-a",
        node_id="member",
        control_node_id="control",
    )
    await store.initialize()
    current = _managed_replica_snapshot(member_mesh_id="mesh-a")
    await store.apply_managed_replica(current)
    stale = current | {"revisions": current["revisions"] | {"topology": 1}}

    with pytest.raises(FleetControlError, match="managed_snapshot_stale"):
        await store.apply_managed_replica(stale)

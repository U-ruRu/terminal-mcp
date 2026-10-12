"""Managed Access issuer trust follows VERIFIED Fleet Control membership.

These tests are intentionally isolated: no server, OAuth DB, Android device or
running work sessions are touched.
"""
import asyncio
from dataclasses import asdict

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.fleet.access_mesh import AccessMeshReplication, PinnedAccessMeshPeerAuth
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


POLICY = SlotPolicy(1200, 60, True, 120, 30)


class FakeStore:
    local_node_id = "firstbyte"

    def __init__(self):
        self.trusted_issuers = frozenset({"firstbyte"})

    def sync_parent_defaults(self, parent):
        return {"policy": asdict(POLICY), "legacy_enabled": True}


def fake_access():
    app = AccessMeshApplication.__new__(AccessMeshApplication)
    app.store = FakeStore()
    app.configured_defaults = POLICY
    app.configured_legacy_enabled = True
    app.defaults = POLICY
    app.legacy_enabled = True
    app._issuer_session_lock = asyncio.Lock()
    app._issuer_lock = asyncio.Lock()
    app._bootstrap_trusted_issuers = frozenset(app.store.trusted_issuers)
    app.managed_trust_enabled = False
    app.managed_trust_mesh_id = None
    return app


def state(*, firstbyte_mesh="mesh-one", bacloud_mesh="mesh-one",
          tokyo_mesh="mesh-one", authority="bacloud"):
    return {
        "managed": True,
        "control_node_id": authority,
        "nodes": [
            {"node_id": "firstbyte", "state": "active", "mesh_id": firstbyte_mesh},
            {"node_id": "bacloud", "state": "active", "mesh_id": bacloud_mesh},
            {"node_id": "tokyo", "state": "active", "mesh_id": tokyo_mesh},
            {"node_id": "outsider", "state": "detached", "mesh_id": "mesh-one"},
        ],
        "policy": {
            "duration_seconds": 1200,
            "rearm_after_seconds": 60,
            "warning_after_seconds": 1080,
            "alert_after_seconds": 1170,
            "revision": 2,
            "legacy_admission_enabled": True,
        },
    }


def config(*peers, local_token=None):
    return FleetConfig(
        "firstbyte", "dummy-private-key",
        tuple(FleetPeer(name, f"https://{name}.example", "pub", f"token-{name}") for name in peers),
        5.0, 3.0, local_token,
    )


def test_verified_join_enrolls_only_same_mesh_members_and_authenticates_peer():
    app = fake_access()
    managed = config("bacloud", "tokyo", "outsider", local_token="token-firstbyte")
    replica = AccessMeshReplication(app, config())
    assert replica.peers == ()

    asyncio.run(app.apply_fleet_policy(state()))
    replica.apply_managed_fleet_config(managed)
    assert app.managed_trust_enabled
    assert app.store.trusted_issuers == frozenset({"firstbyte", "bacloud", "tokyo"})
    assert {p.instance_id for p in replica.peers} == {"bacloud", "tokyo"}
    assert replica.config.local_auth_token == "token-firstbyte"

    inbound = PinnedAccessMeshPeerAuth(config(), replica)
    assert inbound.authenticate("bacloud", "Bearer token-bacloud") is not None
    assert inbound.authenticate("bacloud", "Bearer WRONG") is None
    assert inbound.authenticate("outsider", "Bearer token-outsider") is None


def test_unverified_authority_and_other_mesh_cannot_gain_trust():
    app = fake_access()
    asyncio.run(app.apply_fleet_policy(state(authority="outsider")))
    assert app.store.trusted_issuers == frozenset({"firstbyte"})
    assert not app.managed_trust_enabled
    asyncio.run(app.apply_fleet_policy(state(bacloud_mesh="other", tokyo_mesh="other")))
    assert app.store.trusted_issuers == frozenset({"firstbyte"})


def test_leave_revokes_members_and_restores_standalone_bootstrap():
    app = fake_access()
    replica = AccessMeshReplication(app, config())
    asyncio.run(app.apply_fleet_policy(state()))
    replica.apply_managed_fleet_config(config("bacloud", "tokyo"))
    assert set(app.store.trusted_issuers) == {"firstbyte", "bacloud", "tokyo"}
    asyncio.run(app.apply_fleet_policy({"managed": False}))
    replica.apply_managed_fleet_config(config())
    assert app.store.trusted_issuers == frozenset({"firstbyte"})
    assert not app.managed_trust_enabled
    assert replica.peers == ()


def test_authority_change_and_mesh_transfer_restrict_old_issuer():
    app = fake_access()
    asyncio.run(app.apply_fleet_policy(state()))
    transfer = state(firstbyte_mesh="mesh-two", bacloud_mesh="mesh-one",
                     tokyo_mesh="mesh-two", authority="tokyo")
    asyncio.run(app.apply_fleet_policy(transfer))
    assert app.store.trusted_issuers == frozenset({"firstbyte", "tokyo"})
    assert "bacloud" not in app.store.trusted_issuers

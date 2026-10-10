"""Only explicitly trusted Access Mesh peers may affect Mesh health.

Fleet-wide managed transport includes nodes which have no Access Mesh router.
Their 404s must not produce phantom perpetual catchup_pending status.
"""
from types import SimpleNamespace

from terminal_mcp.fleet.access_mesh import AccessMeshReplication
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


def _config(*ids: str):
    peers = tuple(
        FleetPeer(node, f"https://{node}.example.org", "public-key", "test-token")
        for node in ids
    )
    return FleetConfig("firstbyte", "local-public-key", peers, 3.0, 300.0)


def test_managed_fleet_only_publishes_explicitly_trusted_access_peers():
    mesh = SimpleNamespace(
        store=SimpleNamespace(trusted_issuers=frozenset({"firstbyte", "bacloud"}))
    )
    full_fleet = _config("main", "bacloud", "tokyo", "secondary")
    replica = AccessMeshReplication(mesh, full_fleet)

    assert set(replica.peer_health) == {"bacloud"}
    # A transport refresh containing all Fleet peers must not add peers which
    # have no Access Mesh endpoint and return 404 on its signed routes.
    replica.apply_managed_fleet_config(full_fleet)
    assert set(replica.peer_health) == {"bacloud"}
    assert [p.instance_id for p in replica.config.peers] == ["bacloud"]
    assert [p.instance_id for p in replica.peers] == ["bacloud"]
    assert {p.instance_id for p in full_fleet.peers} == {
        "main", "bacloud", "tokyo", "secondary"
    }

    replica.peer_health["bacloud"] = {"status": "healthy"}
    replica.apply_managed_fleet_config(full_fleet)
    assert replica.peer_health == {"bacloud": {"status": "healthy"}}

    # Disabling Access Mesh transport clears stale health and cursors; a later
    # re-enable must start with a full reconciliation, not the old cursor.
    replica._snapshot_after["bacloud"] = "previous-cursor"
    replica.apply_managed_fleet_config(_config("main", "tokyo", "secondary"))
    assert not replica.peers
    assert replica.peer_health == {}
    assert "bacloud" not in replica._snapshot_after
    assert "bacloud" not in replica._last_snapshot_pass
    replica.apply_managed_fleet_config(full_fleet)
    assert [p.instance_id for p in replica.peers] == ["bacloud"]
    assert replica._snapshot_after["bacloud"] == ""
    assert replica.peer_health["bacloud"]["status"] == "degraded"

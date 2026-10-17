"""Only explicitly trusted Access Mesh peers may affect Mesh health.

Fleet-wide managed transport includes nodes which have no Access Mesh router.
Their 404s must not produce phantom perpetual catchup_pending status.
"""

from types import SimpleNamespace

from terminal_mcp.fleet.access_mesh import AccessMeshReplication
from terminal_mcp.fleet.config import FleetConfig, FleetPeer


def _config(*ids: str):
    peers = tuple(
        FleetPeer(node, f"https://{node}.example.org", "public-key", "test-token") for node in ids
    )
    return FleetConfig("firstbyte", "local-public-key", peers, 3.0, 300.0)


def test_transport_refresh_publishes_only_trusted_access_peer_health():
    mesh = SimpleNamespace(
        store=SimpleNamespace(trusted_issuers=frozenset({"firstbyte", "bacloud"}))
    )
    replica = AccessMeshReplication(mesh, _config("bacloud"))
    replica.peer_health["bacloud"] = {"status": "healthy"}
    replica.apply_managed_fleet_config(_config("main", "bacloud", "tokyo"))
    assert [peer.instance_id for peer in replica.peers] == ["bacloud"]
    assert replica.peer_health == {"bacloud": {"status": "healthy"}}

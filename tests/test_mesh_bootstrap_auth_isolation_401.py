from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.fleet.access_mesh import PinnedAccessMeshPeerAuth, build_access_mesh_router
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.replication import FleetReplicationService


def test_mesh_inbound_pinned_after_managed_fleet_rotates_peer_credentials():
    peer_id = "bacloud"
    base = FleetConfig("firstbyte", "private-key-fixture", (
        FleetPeer(peer_id, "https://test.invalid", "fixture-public", "mesh-bootstrap-pair-key"),
    ), 1.0, 1.0)
    managed = FleetConfig("firstbyte", "private-key-fixture", (
        FleetPeer(peer_id, "https://test.invalid", "fixture-public", "managed-trust-key"),
    ), 1.0, 1.0, "managed-local-key")
    mutable_fleet = FleetReplicationService(
        base, None, None, max_session_seconds=1380
    )
    pinned_mesh_auth = PinnedAccessMeshPeerAuth(base)
    mutable_fleet.config = managed

    assert mutable_fleet.authenticate(peer_id, "Bearer managed-trust-key") is not None
    assert mutable_fleet.authenticate(peer_id, "Bearer mesh-bootstrap-pair-key") is None
    assert pinned_mesh_auth.authenticate(peer_id, "Bearer mesh-bootstrap-pair-key") is not None
    assert pinned_mesh_auth.authenticate(peer_id, "Bearer managed-trust-key") is None

    store = SimpleNamespace(
        trusted_issuers=frozenset({"firstbyte", peer_id}),
        local_node_id="firstbyte",
        snapshot_page=lambda **kwargs: [],
    )
    app = FastAPI()
    app.include_router(build_access_mesh_router(SimpleNamespace(store=store), pinned_mesh_auth))
    with TestClient(app) as client:
        base_headers = {"x-terminal-mcp-peer": peer_id}
        valid = client.post(
            "/internal/fleet/access-mesh/snapshot",
            headers={**base_headers, "authorization": "Bearer mesh-bootstrap-pair-key"},
            json={"after": "", "limit": 1},
        )
        assert valid.status_code == 200 and valid.json() == {"ok": True, "slots": []}
        rotated = client.post(
            "/internal/fleet/access-mesh/snapshot",
            headers={**base_headers, "authorization": "Bearer managed-trust-key"},
            json={"after": "", "limit": 1},
        )
        assert rotated.status_code == 401


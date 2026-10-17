from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.fleet.access_mesh import PinnedAccessMeshPeerAuth, build_access_mesh_router
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.replication import FleetReplicationService


def test_mesh_snapshot_accepts_pinned_bootstrap_after_fleet_rotation():
    peer_id = "bacloud"
    base = FleetConfig(
        "firstbyte",
        "private-key-fixture",
        (FleetPeer(peer_id, "https://test.invalid", "fixture-public", "mesh-bootstrap-pair-key"),),
        1.0,
        1.0,
    )
    managed = FleetConfig(
        "firstbyte",
        "private-key-fixture",
        (FleetPeer(peer_id, "https://test.invalid", "fixture-public", "managed-trust-key"),),
        1.0,
        1.0,
        "managed-local-key",
    )
    mutable_fleet = FleetReplicationService(base, None, None, max_session_seconds=1380)
    pinned_mesh_auth = PinnedAccessMeshPeerAuth(base)
    mutable_fleet.config = managed

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

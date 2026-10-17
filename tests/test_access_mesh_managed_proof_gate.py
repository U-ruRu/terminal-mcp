"""Isolated authenticated proof-gate tests. No live server or credentials."""

from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.fleet.access_mesh import (
    AccessMeshReplication,
    PinnedAccessMeshPeerAuth,
    build_access_mesh_router,
)
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

PROOF = b"managed-native-proof-gate-test-32-byte-key"
OTHER_PROOF = b"another-different-native-proof-key-xxx"


@dataclass
class Fence:
    async def revoke_session(self, *args, **kwargs):
        return []


class Routes(httpx.AsyncBaseTransport):
    def __init__(self):
        self.apps = {}
        self.offline = set()

    async def handle_async_request(self, request):
        if request.url.host in self.offline:
            raise httpx.ConnectError("offline fixture", request=request)
        transport = httpx.ASGITransport(app=self.apps[request.url.host])
        return await transport.handle_async_request(request)


async def setup(tmp_path, name, other, key, routes, client):
    path = tmp_path / (name + ".sqlite3")
    await SqliteRepository(path, tmp_path / (name + "-out.sqlite3")).initialize()
    store = AccessMeshStore(
        path, local_node_id=name, trusted_issuers=frozenset((name, other)), proof_key=key
    )
    store.install_registry_scope(mesh_id="mesh-test", authority_id="bacloud")
    mesh = AccessMeshApplication(store, task_store=TaskStore(path), execution_fence=Fence())
    mesh.managed_trust_enabled = True
    mesh.managed_trust_mesh_id = "mesh-test"
    # Explicitly enroll the synthetic authority proof. Different fixture
    # tokens intentionally simulate an incompatible managed enrollment.
    store.install_managed_proof(
        mesh_id="mesh-test",
        authority_id="bacloud",
        ingress_token=key.decode(),
    )
    peer = FleetPeer(other, "https://" + other, "test-public", "token-" + other)
    config = FleetConfig(name, "private-fixture", (peer,), 1.0, 1.0, "token-" + name)
    replica = AccessMeshReplication(mesh, config, client=client)
    mesh.replication = replica
    app = FastAPI()
    app.include_router(build_access_mesh_router(mesh, PinnedAccessMeshPeerAuth(config)))
    routes.apps[name] = app
    return mesh, replica


@pytest.mark.asyncio
async def test_authenticated_peer_receives_nonce_bound_proof(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        mesh, _ = await setup(tmp_path, "firstbyte", "bacloud", PROOF, routes, client)
        response = await client.post(
            "https://firstbyte/internal/fleet/access-mesh/proof",
            headers={
                "x-terminal-mcp-peer": "bacloud",
                "authorization": "Bearer token-bacloud",
                "x-terminal-mcp-mesh": "mesh-test",
            },
            json={"nonce": "a" * 32},
        )
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "receiver_id": "firstbyte",
            "proof": mesh.store.peer_proof_tag(
                receiver_id="firstbyte", sender_id="bacloud", nonce="a" * 32
            ),
        }


@pytest.mark.asyncio
async def test_mismatched_peer_proof_remains_unverified(tmp_path, monkeypatch):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        _, replication = await setup(tmp_path, "firstbyte", "bacloud", PROOF, routes, client)

        async def wrong_proof(peer, path, payload):
            return {"ok": True, "receiver_id": "bacloud", "proof": "0" * 64}

        monkeypatch.setattr(replication, "request", wrong_proof)
        assert not await replication.verify_peer_proof(replication.peers[0])
        assert replication.native_trust_status()["peers"] == {"bacloud": "proof_mismatch"}

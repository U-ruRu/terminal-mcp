"""Isolated authenticated proof-gate tests. No live server or credentials."""
import asyncio
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
        return await httpx.ASGITransport(app=self.apps[request.url.host]).handle_async_request(request)


async def setup(tmp_path, name, other, key, routes, client):
    path = tmp_path / (name + ".sqlite3")
    await SqliteRepository(path, tmp_path / (name + "-out.sqlite3")).initialize()
    store = AccessMeshStore(
        path, local_node_id=name,
        trusted_issuers=frozenset((name, other)), proof_key=key
    )
    mesh = AccessMeshApplication(
        store, task_store=TaskStore(path), execution_fence=Fence()
    )
    mesh.managed_trust_enabled = True
    mesh.managed_trust_mesh_id = "mesh-test"
    peer = FleetPeer(other, "https://" + other, "test-public", "token-" + other)
    config = FleetConfig(name, "private-fixture", (peer,), 1.0, 1.0, "token-" + name)
    replica = AccessMeshReplication(mesh, config, client=client)
    mesh.replication = replica
    app = FastAPI()
    app.include_router(build_access_mesh_router(mesh, PinnedAccessMeshPeerAuth(config)))
    routes.apps[name] = app
    return mesh, replica


@pytest.mark.asyncio
async def test_proof_http_requires_authenticated_known_peer_and_valid_nonce(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        mesh, _ = await setup(tmp_path, "firstbyte", "bacloud", PROOF, routes, client)
        headers = {"x-terminal-mcp-peer": "bacloud", "authorization": "Bearer token-bacloud"}
        url = "https://firstbyte/internal/fleet/access-mesh/proof"
        valid = await client.post(url, headers=headers, json={"nonce": "a" * 32})
        assert valid.status_code == 200
        assert valid.json() == {
            "ok": True, "receiver_id": "firstbyte",
            "proof": mesh.store.peer_proof_tag(
                receiver_id="firstbyte", sender_id="bacloud", nonce="a" * 32
            )
        }
        invalid_token = await client.post(
            url, headers={**headers, "authorization": "Bearer wrong"},
            json={"nonce": "a" * 32},
        )
        assert invalid_token.status_code == 401
        invalid_issuer = await client.post(
            url, headers={**headers, "x-terminal-mcp-peer": "outsider"},
            json={"nonce": "a" * 32},
        )
        assert invalid_issuer.status_code == 401
        malformed = await client.post(url, headers=headers, json={"nonce": "a" * 31})
        assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_proof_gate_detects_mismatched_secret_recovers_and_rechecks_after_restart(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        bac, rep = await setup(tmp_path, "bacloud", "firstbyte", PROOF, routes, client)
        other, _ = await setup(tmp_path, "firstbyte", "bacloud", OTHER_PROOF, routes, client)
        assert rep.native_trust_status()["state"] == "pending"
        assert not await rep.verify_peer_proof(rep.peers[0])
        assert rep.native_trust_status()["peers"] == {"firstbyte": "proof_mismatch"}
        await rep.sync_peer(rep.peers[0])
        assert rep.peer_health["firstbyte"]["status"] == "degraded"

        # A valid authenticated peer with the same non-exported proof can recover.
        other.store._proof_key = PROOF
        assert await rep.verify_peer_proof(rep.peers[0])
        assert rep.native_trust_status()["state"] == "ready"

        # Restart discards the cached confirmation; a fresh proof is required.
        restarted = AccessMeshReplication(bac, rep.config, client=client)
        bac.replication = restarted
        assert restarted.native_trust_status()["state"] == "pending"
        routes.offline.add("firstbyte")
        assert not await restarted.verify_peer_proof(restarted.peers[0])
        assert restarted.native_trust_status()["state"] == "pending"
        routes.offline.clear()
        assert await restarted.verify_peer_proof(restarted.peers[0])
        assert restarted.native_trust_status()["state"] == "ready"

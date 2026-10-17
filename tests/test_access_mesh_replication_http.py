from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.fleet.access_mesh import AccessMeshReplication, build_access_mesh_router
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KEY = b"replication-tests-only-never-use-in-production"


class Fence:
    async def revoke_session(self, *args, **kwargs):
        return []


class Routes(httpx.AsyncBaseTransport):
    def __init__(self):
        self.apps = {}
        self.offline = set()

    async def handle_async_request(self, request):
        if request.url.host in self.offline:
            raise httpx.ConnectError("fixture offline", request=request)
        return await httpx.ASGITransport(app=self.apps[request.url.host]).handle_async_request(
            request
        )


def actor():
    return ActorContext(
        auth_mode="bearer",
        principal_id="fixture-user",
        credential_id="fixture",
        auth_generation=1,
        scopes=frozenset({"terminal:read", "terminal:execute"}),
        provider="openai",
        provider_metadata={"openai/subject": "s", "openai/session": "c"},
        transport="mcp",
        endpoint_role="executor",
    )


async def node(tmp_path, name, other, routes, client, *, proof_key=KEY):
    path = tmp_path / f"{name}.db"
    await SqliteRepository(path, tmp_path / f"{name}-out.db").initialize()
    store = AccessMeshStore(
        path, local_node_id=name, trusted_issuers=frozenset({name, other}), proof_key=proof_key
    )
    store.install_registry_scope(mesh_id="fixture-mesh", authority_id="firstbyte")
    mesh = AccessMeshApplication(
        store, task_store=TaskStore(path), execution_fence=Fence(), clock=lambda: T0
    )
    peer = FleetPeer(other, f"https://{other}", "fixture-key", "fixture-token")
    config = FleetConfig(name, "fixture-key", (peer,), 1.0, 1.0)

    def authenticate(peer_id, authorization):
        return peer if peer_id == other and authorization == "Bearer fixture-token" else None

    app = FastAPI()
    app.include_router(build_access_mesh_router(mesh, SimpleNamespace(authenticate=authenticate)))
    routes.apps[name] = app
    replica = AccessMeshReplication(mesh, config, client=client)
    return mesh, replica


@pytest.mark.asyncio
async def test_authenticated_peer_cannot_spoof_event_issuer(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issued = await source.issue(actor(), code="1234", kind="persistent")
        wire = source.store.pending_outbox(peer_node_id="bacloud")[0].event.to_wire()
        response = await client.post(
            "https://firstbyte/internal/fleet/access-mesh/events",
            headers={
                "x-terminal-mcp-peer": "bacloud",
                "authorization": "Bearer fixture-token",
                "x-terminal-mcp-mesh": "fixture-mesh",
            },
            json={"events": [{"event": wire, "issued_kind": "persistent"}]},
        )
        assert response.json()["code"] == "access_mesh_untrusted_issuer"
        assert source.store.slot("firstbyte", issued["slot_id"]).revision == 1


@pytest.mark.asyncio
async def test_http_delivery_applies_committed_event_and_acknowledges_outbox(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        target, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issued = await source.issue(actor(), code="1234", kind="persistent")
        assert await replication._deliver(replication.peers[0])
        assert (
            target.store.slot("firstbyte", issued["slot_id"]).logical_agent_id
            == issued["logical_agent_id"]
        )
        assert source.store.pending_outbox(peer_node_id="bacloud") == ()

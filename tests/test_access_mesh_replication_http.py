import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedOperation
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


async def node(tmp_path, name, other, routes, client):
    path = tmp_path / f"{name}.db"
    await SqliteRepository(path, tmp_path / f"{name}-out.db").initialize()
    store = AccessMeshStore(
        path, local_node_id=name, trusted_issuers=frozenset({name, other}), proof_key=KEY
    )
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
async def test_http_two_issuers_and_offline_hot_path(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        fb, fb_rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        bac, bac_rep = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        a = await fb.issue(actor(), code="1234")
        b = await bac.issue(actor(), code="1234")
        await fb_rep.tick()
        await bac_rep.tick()
        f = await fb.attach(actor(), issuer_node_id="bacloud", access_code="1234")
        c = await bac.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
        assert f["logical_agent_id"] == b["logical_agent_id"]
        assert c["logical_agent_id"] == a["logical_agent_id"]
        assert not fb.store.pending_outbox(peer_node_id="bacloud")
        routes.offline.add("firstbyte")
        bac_rep._last_snapshot_pass["firstbyte"] = 0
        await bac_rep.tick()
        assert bac_rep.peer_health["firstbyte"]["status"] == "degraded"
        local = await bac.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert local["work_session_id"] == c["work_session_id"]
        assert (await bac.issue(actor(), code="5678"))["ok"] is True


@pytest.mark.asyncio
async def test_gap_catches_up_by_authenticated_snapshot_and_tombstone(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        fb, rep = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        bac, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        issued = await fb.issue(actor(), code="1234")
        await rep.tick()
        await bac.attach(actor(), issuer_node_id="firstbyte", access_code="1234")
        await fb.change(
            actor(), slot_id=issued["slot_id"], kind="SlotSuspended", expected_revision=1
        )
        missing = fb.store.pending_outbox(peer_node_id="bacloud")[0]
        # A restored receiver can miss an earlier event that the issuer had ACKed.
        fb.store.acknowledge_delivery(
            peer_node_id="bacloud", event_id=missing.event.event_id, authenticated_peer_id="bacloud"
        )
        await fb.change(actor(), slot_id=issued["slot_id"], kind="SlotResumed", expected_revision=2)
        await rep.tick()
        assert bac.store.slot("firstbyte", issued["slot_id"]).revision == 3
        await rep.tick()
        assert not fb.store.pending_outbox(peer_node_id="bacloud")
        await fb.change(actor(), slot_id=issued["slot_id"], kind="SlotDeleted", expected_revision=3)
        await rep.tick()
        assert bac.store.slot("firstbyte", issued["slot_id"]).state == "deleted"
        assert bac.store.attached_slot(bac.connection_key(actor())) is None


@pytest.mark.asyncio
async def test_peer_authentication_cannot_be_replaced_by_event_issuer_field(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        fb, _ = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        issued = await fb.issue(actor(), code="1234")
        wire = fb.store.pending_outbox(peer_node_id="bacloud")[0].event.to_wire()
        response = await client.post(
            "https://firstbyte/internal/fleet/access-mesh/events",
            json={"events": [{"event": wire, "issued_kind": "legacy"}]},
        )
        assert response.status_code == 401
        response = await client.post(
            "https://firstbyte/internal/fleet/access-mesh/events",
            headers={"x-terminal-mcp-peer": "bacloud", "authorization": "Bearer fixture-token"},
            json={"events": [{"event": wire, "issued_kind": "legacy"}]},
        )
        assert response.json()["code"] == "access_mesh_untrusted_issuer"
        assert fb.store.slot("firstbyte", issued["slot_id"]).revision == 1


@pytest.mark.asyncio
async def test_idle_mesh_avoids_network_polling_and_wakes_for_committed_events(tmp_path):
    """After bootstrap, no HTTP requests are sent until an event is committed."""

    class CountingRoutes(Routes):
        def __init__(self):
            super().__init__()
            self.paths = []

        async def handle_async_request(self, request):
            self.paths.append(request.url.path)
            return await super().handle_async_request(request)

    routes = CountingRoutes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        destination, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        await replication.start()
        try:

            async def eventually(predicate):
                async def wait():
                    while not predicate():  # noqa: ASYNC110 - isolated background delivery
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait(), 2)

            await eventually(lambda: replication.peer_health["bacloud"]["status"] == "healthy")
            assert "/internal/fleet/access-mesh/snapshot" in routes.paths
            routes.paths.clear()
            await asyncio.sleep(0.05)
            assert routes.paths == []

            issued = await source.issue(actor(), code="1234")
            await eventually(
                lambda: destination.store.slot("firstbyte", issued["slot_id"]) is not None
            )
            assert "/internal/fleet/access-mesh/events" in routes.paths
            assert "/internal/fleet/access-mesh/snapshot" not in routes.paths
            assert replication._last_snapshot_pass["bacloud"] > 0
        finally:
            await replication.stop()
            assert source.store._delivery_notifier is None


@pytest.mark.asyncio
async def test_anti_entropy_snapshot_interval_is_at_least_five_minutes(tmp_path):
    class CountingRoutes(Routes):
        def __init__(self):
            super().__init__()
            self.snapshots = 0

        async def handle_async_request(self, request):
            if request.url.path.endswith("/access-mesh/snapshot"):
                self.snapshots += 1
            return await super().handle_async_request(request)

    routes = CountingRoutes()
    async with httpx.AsyncClient(transport=routes) as client:
        _, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        await node(tmp_path, "bacloud", "firstbyte", routes, client)
        await replication.tick()
        assert routes.snapshots == 1
        await replication.tick()
        assert routes.snapshots == 1
        replication._last_snapshot_pass["bacloud"] -= 301
        await replication.tick()
        assert routes.snapshots == 2


@pytest.mark.asyncio
async def test_startup_drains_more_than_one_outbox_page_without_periodic_polling(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, replication = await node(tmp_path, "firstbyte", "bacloud", routes, client)
        destination, _ = await node(tmp_path, "bacloud", "firstbyte", routes, client)
        slots = []
        for code in range(3100, 3125):
            slots.append((await source.issue(actor(), code=str(code)))["slot_id"])
        await replication.start()
        try:

            async def delivered():
                while any(destination.store.slot("firstbyte", slot) is None for slot in slots):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(delivered(), 4)

            async def acknowledged():
                while source.store.pending_outbox(peer_node_id="bacloud"):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(acknowledged(), 4)
        finally:
            await replication.stop()

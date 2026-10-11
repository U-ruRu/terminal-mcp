"""Isolated trusted-fleet lifecycle interest, restart and delivery contracts."""

import base64
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.fleet.access_mesh import (
    AccessMeshReplication,
    PinnedAccessMeshPeerAuth,
    build_access_mesh_router,
)
from terminal_mcp.fleet.config import FleetConfig, FleetPeer
from terminal_mcp.fleet.event_mesh import (
    MAX_PEER_ATTEMPTS,
    LifecycleEventMesh,
    build_lifecycle_event_router,
)
from terminal_mcp.fleet.message_event_mesh import build_message_event_router
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore

SECRET = "whsec_" + base64.b64encode(b"isolated-fixture-key-only-12345678").decode()
KEY = b"isolated-mesh-proof-fixture-key-only"


class Routes(httpx.AsyncBaseTransport):
    def __init__(self):
        self.apps = {}
        self.offline = set()
        self.drop_ack = set()
        self.requests = []

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.host, request.url.path, body))
        if request.url.host in self.offline:
            raise httpx.ConnectError("isolated fixture offline", request=request)
        response = await httpx.ASGITransport(app=self.apps[request.url.host]).handle_async_request(
            request
        )
        key = (request.url.host, request.url.path)
        if key in self.drop_ack:
            self.drop_ack.remove(key)
            raise httpx.ConnectError("isolated lost acknowledgement", request=request)
        return response


class Fence:
    async def revoke_session(self, *args, **kwargs):
        return []


async def node(tmp_path, name, names, routes, client):
    path = tmp_path / (name + ".db")
    await SqliteRepository(path, tmp_path / (name + "-output.db")).initialize()
    events = MCPEventStore(path, name)
    await events.initialize()
    store = AccessMeshStore(
        path,
        local_node_id=name,
        trusted_issuers=frozenset(names),
        proof_key=KEY,
    )
    mesh = AccessMeshApplication(store, task_store=TaskStore(path), execution_fence=Fence())
    peers = tuple(
        FleetPeer(other, "https://" + other, "isolated-key", "isolated-token")
        for other in names
        if other != name
    )
    config = FleetConfig(name, "isolated-key", peers, 1.0, 1.0)
    replication = AccessMeshReplication(mesh, config, client=client)
    lifecycle = LifecycleEventMesh(events, replication.request, authorization_checker=allowed)
    replication.lifecycle_events = lifecycle
    auth = PinnedAccessMeshPeerAuth(config)
    app = FastAPI()
    app.include_router(build_access_mesh_router(mesh, auth))
    app.include_router(build_lifecycle_event_router(lifecycle, auth, store.trusted_issuers))
    app.include_router(
        build_message_event_router(lifecycle.message_events, auth, store.trusted_issuers)
    )
    routes.apps[name] = app
    return SimpleNamespace(
        path=path,
        events=events,
        lifecycle=lifecycle,
        replication=replication,
        mesh=mesh,
        config=config,
        client=client,
    )


async def allowed(item):
    return True


async def subscribe(n, sub_id, name="agent.attached", ttl_ms=3600000):
    return await n.events.save_subscription(
        sub_id=sub_id,
        principal="fixture-user",
        credential_id="bearer:fixture",
        auth_mode="bearer",
        name=name,
        callback="https://callback.invalid/" + sub_id,
        secret=SECRET,
        ttl_ms=ttl_ms,
    )


async def attach(n, agent_id="Alpha-12345678", seconds_ago=1):
    await AgentStore(n.path).create_session(
        agent_id,
        "private work must remain private",
        "private intent",
        [],
        ["private step"],
        1,
        utc_text(utc_now() - timedelta(seconds=seconds_ago)),
        source_instance_id="origin-fixture",
        global_expires_at=utc_text(utc_now() + timedelta(seconds=1000)),
    )
    await n.events.pump()


def records(n):
    with sqlite3.connect(n.path) as db:
        return db.execute(
            "SELECT event_id,name,occurred_at,data_json,origin_node_id "
            "FROM mcp_event_records ORDER BY event_id"
        ).fetchall()


def outbox(n):
    with sqlite3.connect(n.path) as db:
        return db.execute(
            "SELECT event_id,peer_node_id,state,attempts FROM mcp_event_mesh_outbox"
        ).fetchall()


@pytest.mark.asyncio
async def test_number_issuance_never_subscribes_or_forwards(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        actor = ActorContext(
            auth_mode="bearer",
            principal_id="fixture-user",
            credential_id="fixture",
            auth_generation=1,
            scopes=frozenset({"terminal:read", "terminal:execute"}),
            provider="openai",
            provider_metadata={"openai/subject": "s", "openai/session": "c"},
            transport="mcp",
            endpoint_role="access",
        )
        await access.mesh.issue(actor, code="1234")
        await access.replication.tick()
        assert (await access.lifecycle.local_interest())["event_names"] == []
        await attach(source)
        await source.replication.tick()
        assert not records(access)
        assert not outbox(source)
        assert not any(path.endswith("lifecycle/events") for _, path, _ in routes.requests)


@pytest.mark.asyncio
async def test_aggregate_first_one_last_unsubscribe_and_expiry(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(access, "one")
        first = await access.lifecycle.local_interest()
        await subscribe(access, "two")
        assert await access.lifecycle.local_interest() == first
        await access.replication.tick()
        with sqlite3.connect(source.path) as db:
            assert db.execute("SELECT count(*) FROM mcp_event_mesh_interest").fetchone()[0] == 1
        await access.events.unsubscribe("one", "fixture-user")
        assert await access.lifecycle.local_interest() == first
        await access.replication.tick()
        await access.events.unsubscribe_matching(
            "fixture-user",
            "agent.attached",
            "https://callback.invalid/two",
        )
        last = await access.lifecycle.local_interest()
        assert last["event_names"] == [] and last["version"] == first["version"] + 1
        await access.replication.tick()
        with sqlite3.connect(source.path) as db:
            assert not db.execute("SELECT * FROM mcp_event_mesh_interest").fetchall()
        await subscribe(access, "expires", ttl_ms=1000)
        current = await access.lifecycle.local_interest()
        await source.lifecycle.receive_interest("access", current)
        expired = await access.lifecycle.local_interest(now=utc_now() + timedelta(seconds=2))
        assert expired["event_names"] == []
        await source.lifecycle.receive_interest("access", expired)
        await attach(source)
        await source.replication.tick()
        assert not outbox(source)


@pytest.mark.asyncio
async def test_local_and_remote_fanout_preserves_source_metadata_and_secrets(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(source, "local")
        await subscribe(access, "remote-one")
        await subscribe(access, "remote-two")
        await access.replication.tick()
        await attach(source)
        await source.replication.tick()
        await access.events.pump()
        originals = records(source)
        forwarded = records(access)
        assert len(originals) == len(forwarded) == 1
        assert originals[0][:4] == forwarded[0][:4]
        assert forwarded[0][4] == "source"
        observed = []

        async def sender(url, body, headers):
            observed.append((url, json.loads(body), headers))
            return 200, b"{}"

        for n in (source, access):
            adapter = MCPEventsAdapter(None, n.events, sender=sender, access_checker=allowed)
            await adapter.deliver_due()
            await adapter.deliver_due()
        assert len(observed) == 3
        assert len({body["eventId"] for _, body, _ in observed}) == 1
        assert all(
            body["data"] == {"agent_name": "Alpha", "server": "source"} for _, body, _ in observed
        )
        wire = json.dumps([body for _, path, body in routes.requests if "/lifecycle/" in path])
        assert SECRET not in wire and "callback.invalid" not in wire and "private" not in wire


@pytest.mark.asyncio
async def test_three_nodes_type_selection_and_local_inactivity_does_not_relay(tmp_path):
    routes = Routes()
    names = ("source", "access", "third")
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", names, routes, client)
        access = await node(tmp_path, "access", names, routes, client)
        third = await node(tmp_path, "third", names, routes, client)
        await subscribe(access, "idle-only", "agent.inactive")
        await subscribe(third, "attach-only")
        await access.replication.tick()
        await third.replication.tick()
        await attach(source, seconds_ago=190)
        await source.replication.tick()
        await access.events.pump()
        await third.events.pump()
        assert [r[1] for r in records(access)] == ["agent.inactive"]
        assert [r[1] for r in records(third)] == ["agent.attached"]
        await access.replication.tick()
        await third.replication.tick()
        assert not outbox(access) and not outbox(third)
        assert all(json.loads(r[3])["server"] == "source" for r in records(access) + records(third))
        # Replicated public inactivity is an envelope only, never a local idle clock.
        with sqlite3.connect(access.path) as db:
            assert not db.execute("SELECT * FROM mcp_event_managed_activity").fetchall()
            assert not db.execute("SELECT * FROM agent_sessions").fetchall()


@pytest.mark.asyncio
async def test_restart_pending_retry_lost_ack_dedup_and_existing_subscription(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(access, "durable")
        await access.replication.tick()
        await attach(source)
        routes.offline.add("access")
        await source.replication.tick()
        # The source queues/claims only after the interest announcement succeeds;
        # when peer HTTP is offline, durable records and interest still survive.
        routes.offline.clear()
        routes.drop_ack.add(("access", "/internal/fleet/access-mesh/lifecycle/events"))
        await source.replication.tick()
        assert len(records(access)) == 1
        assert outbox(source)[0][2] == "pending"
        original_id = records(access)[0][0]
        with sqlite3.connect(source.path) as db:
            db.execute(
                "UPDATE mcp_event_mesh_outbox SET state='sending',next_attempt_at=?", (utc_text(),)
            )
        await source.events.initialize()
        await access.events.initialize()
        # Ordinary repository/schema upgrade path is idempotent and retains identity.
        await SqliteRepository(access.path).initialize()
        await access.events.initialize()
        restored = AccessMeshReplication(source.mesh, source.config, client=client)
        restored.lifecycle_events = LifecycleEventMesh(
            source.events, restored.request, authorization_checker=allowed
        )
        await restored.tick()
        assert len(records(access)) == 1 and records(access)[0][0] == original_id
        assert outbox(source)[0][2] == "sent"
        await access.events.pump()
        due = await access.events.due_deliveries()
        assert len(due) == 1 and due[0]["subscription_id"] == "durable"
        assert due[0]["event_id"] == original_id
        snapshot = await LifecycleEventMesh(
            access.events, access.replication.request
        ).local_interest()
        assert snapshot["event_names"] == ["agent.attached"]


@pytest.mark.asyncio
async def test_lease_outage_pauses_existing_delivery_and_restart_reconciles(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(access, "durable")
        snapshot = await access.lifecycle.local_interest()
        await source.lifecycle.receive_interest("access", snapshot)
        await attach(source)
        pending = await source.lifecycle.due("access")
        await source.lifecycle.finish("access", [e["eventId"] for e in pending], set())
        with sqlite3.connect(source.path) as db:
            db.execute("UPDATE mcp_event_mesh_interest SET expires_at=?", (utc_text(),))
            db.execute("UPDATE mcp_event_mesh_outbox SET next_attempt_at=?", (utc_text(),))
        assert not await source.lifecycle.due("access")
        assert outbox(source)[0][2] == "pending"
        await source.lifecycle.receive_interest("access", snapshot)
        assert (await source.lifecycle.due("access"))[0]["eventId"] == pending[0]["eventId"]
        restarted = LifecycleEventMesh(access.events, access.replication.request)
        await restarted.sync_peer(access.config.peers[0])
        assert any(path.endswith("lifecycle/interest") for _, path, _ in routes.requests)


@pytest.mark.asyncio
async def test_attempts_retention_revoked_access_and_no_work_session_liveness_dependency(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(access, "persistent")
        await access.replication.tick()
        await attach(source)
        with sqlite3.connect(source.path) as db:
            # Represents an unrelated work window ending. Subscription identity is
            # credential-bound; neither the interest nor callback checks use it.
            db.execute("UPDATE agent_sessions SET state='forced',end_reason='hard_expiry'")
        assert (await access.lifecycle.local_interest())["event_names"] == ["agent.attached"]
        pending = await source.lifecycle.due("access")
        with sqlite3.connect(source.path) as db:
            db.execute("UPDATE mcp_event_mesh_outbox SET attempts=?", (MAX_PEER_ATTEMPTS,))
        await source.lifecycle.finish("access", [e["eventId"] for e in pending], set())
        assert outbox(source)[0][2] == "dead"
        await source.lifecycle.due("access", now=utc_now() + timedelta(days=2))
        assert not outbox(source)
        assert records(source)  # Existing local source-retention semantics unchanged.

        async def revoked(item):
            return False

        access.lifecycle.authorization_checker = revoked
        assert (await access.lifecycle.local_interest())["event_names"] == []


@pytest.mark.asyncio
async def test_authenticated_source_only_strict_wire_and_stale_interest(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await subscribe(access, "one")
        base = "https://access/internal/fleet/access-mesh/lifecycle/"
        snapshot = {
            "version": 2,
            "event_names": ["agent.attached"],
            "started_at": {"agent.attached": utc_text()},
        }
        assert (await client.post(base + "interest", json=snapshot)).status_code == 401
        headers = {"X-Terminal-MCP-Peer": "source", "Authorization": "Bearer isolated-token"}
        assert (
            await client.post(base + "interest", json=snapshot, headers=headers)
        ).status_code == 200
        await access.lifecycle.receive_interest(
            "source", {"version": 3, "event_names": [], "started_at": {}}
        )
        await access.lifecycle.receive_interest("source", snapshot)
        with sqlite3.connect(access.path) as db:
            assert not db.execute("SELECT * FROM mcp_event_mesh_interest").fetchall()
        event = {
            "eventId": "evt_" + "a" * 32,
            "name": "agent.attached",
            "timestamp": utc_text(),
            "data": {"server": "source", "agent_name": "Bravo-3 | test"},
        }
        good = await client.post(base + "events", json={"events": [event]}, headers=headers)
        assert good.json()["acked"] == [event["eventId"]]
        for mutated in (
            {**event, "name": "message.received"},
            {**event, "callback_url": "https://callback.invalid"},
            {**event, "data": {**event["data"], "server": "third"}},
            {**event, "data": {**event["data"], "signing_secret": SECRET}},
        ):
            response = await client.post(
                base + "events", json={"events": [mutated]}, headers=headers
            )
            assert response.status_code == 400
        assert len(records(access)) == 1
        assert json.loads(records(access)[0][3])["agent_name"] == "Bravo-3 | test"


async def message_subscription(access, slot):
    from terminal_mcp.mcp.message_binding import MessageSubscriber

    await access.events.save_subscription(
        sub_id="private",
        principal="fixture-user",
        credential_id="bearer:fixture",
        auth_mode="bearer",
        name="message.received",
        callback="https://callback.invalid/private",
        secret=SECRET,
        arguments={"session_number": "1234"},
        message_binding=MessageSubscriber(
            slot["logical_agent_id"],
            "fixture-ended-window",
            1,
            "number:1234",
            "access",
            slot["slot_id"],
            "session_number",
        ),
        authorization_checker=allowed,
    )


def local_message(source, recipient, key, text):
    from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore

    store = AccessMeshMessageStore(source.path, "source")
    wire = {
        "message_hash": "source:localmsg:" + key,
        "origin_node_id": "source",
        "sender_id": "fixture-sender",
        "sender_name": "Bravo",
        "target": "Alpha",
        "text": text,
        "created_at": utc_text(),
        "require_reply": False,
        "alert": False,
        "mode": "notify",
        "namespace": None,
        "task_id": None,
        "reply_to": None,
        "scope": "local",
    }
    store.accept(wire, [{"logical_agent_id": recipient, "public_name": "Alpha"}])
    return wire


@pytest.mark.asyncio
async def test_initial_lifecycle_boundary_catches_gap_but_not_pre_subscription(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        await attach(source, "Alpha-11111111")
        await subscribe(access, "one")
        await attach(source, "Bravo-22222222")
        # Announce only after both source events committed.
        await access.replication.tick()
        await source.replication.tick()
        assert len(records(access)) == 1
        assert json.loads(records(access)[0][3])["agent_name"] == "Bravo"


@pytest.mark.asyncio
async def test_message_event_initial_gap_issuer_binding_end_restart_and_unsubscribe(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source = await node(tmp_path, "source", ("source", "access"), routes, client)
        access = await node(tmp_path, "access", ("source", "access"), routes, client)
        actor = ActorContext(
            auth_mode="bearer",
            principal_id="fixture-user",
            credential_id="fixture",
            auth_generation=1,
            scopes=frozenset({"terminal:read", "terminal:execute"}),
            provider="openai",
            provider_metadata={"openai/subject": "s", "openai/session": "c"},
            transport="mcp",
            endpoint_role="access",
        )
        slot = await access.mesh.issue(actor, code="1234")
        await access.replication.tick()
        local_message(source, slot["logical_agent_id"], "before", "pre-subscribe private")
        await source.events.pump()
        assert not [r for r in records(source) if r[1] == "message.received"]
        await message_subscription(access, slot)
        original = local_message(
            source, slot["logical_agent_id"], "after", "bounded authorized private"
        )
        await source.events.pump()
        # Source had no received-interest yet, so only initial bounded backfill
        # may capture the post-subscription message.
        assert not [r for r in records(source) if r[1] == "message.received"]
        await access.replication.tick()
        routes.drop_ack.add(("access", "/internal/fleet/access-mesh/message-events/events"))
        await source.replication.tick()
        imported = [r for r in records(access) if r[1] == "message.received"]
        assert len(imported) == 1
        data = json.loads(imported[0][3])
        assert data["text"] == original["text"] and data["message_id"] == original["message_hash"]
        assert data["timestamp"] == original["created_at"] and data["server"] == "source"
        with sqlite3.connect(source.path) as db:
            db.execute("UPDATE mcp_event_mesh_outbox SET next_attempt_at=?", (utc_text(),))
        await source.events.initialize()
        await access.events.initialize()
        await source.replication.tick()
        assert len([r for r in records(access) if r[1] == "message.received"]) == 1
        await access.events.pump()
        due = await access.events.due_deliveries()
        assert len(due) == 1 and due[0]["event_id"] == imported[0][0]
        assert due[0]["recipient_agent_id"] == slot["logical_agent_id"]
        await access.events.unsubscribe("private", "fixture-user")
        await access.replication.tick()
        local_message(source, slot["logical_agent_id"], "removed", "must not capture")
        await source.events.pump()
        assert len([r for r in records(source) if r[1] == "message.received"]) == 1
        private_wires = [b for _, p, b in routes.requests if "/message-events/" in p]
        assert SECRET not in json.dumps(private_wires)
        assert "callback.invalid" not in json.dumps(private_wires)

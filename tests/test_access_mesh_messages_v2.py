"""Two independent local databases; no route cache or issuer read service."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.application.access_mesh_messages import (
    AccessMeshMessaging,
    _public_name,
)
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.http.access_mesh_messages import build_access_mesh_message_router
from terminal_mcp.storage.access_mesh_messages import MeshMessagingError
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class LocalSlots:
    def __init__(self, node_id, slots, attached):
        self.local_node_id, self.slots, self.attached = node_id, slots, attached
        # This attachment-only double has no Event subscriptions or holds.
        # Production AccessMeshStore always owns the durable hold component.
        self.recipient_holds = SimpleNamespace(local_recipients=lambda: [])

    def local_slots(self, *, after="", limit=50):
        return sorted(
            (
                s
                for s in self.slots.values()
                if s.logical_agent_id in self.attached and s.logical_agent_id > after
            ),
            key=lambda s: s.logical_agent_id,
        )[:limit]

    def canonical_slot(self, slot):
        # This fixture has no collisions: the incoming slot is its own winner.
        # Production AccessMeshStore implements this alias-resolution method.
        return slot

    def human_name(self, slot, *, short=False):
        # This transport fixture has no labels. Naming itself is covered by
        # test_mesh_human_names using the production issuance and serializers.
        return _public_name(slot)

    def slot_for_agent(self, agent_id):
        return self.slots.get(agent_id)

    def local_identity(self, slot, *, now):
        active = slot.active and slot.expires_at > now
        cycle = {
            "state": "active" if active else "expired",
            "started_at": utc_text(slot.started_at),
            "hard_expires_at": utc_text(slot.expires_at),
            "remaining_seconds": max(0, int((slot.expires_at - now).total_seconds())),
        }
        result = {
            "ok": True,
            "logical_agent_id": slot.logical_agent_id,
            "public_name": _public_name(slot),
            "cleanup_pending": False,
            "hard_expires_at": cycle["hard_expires_at"],
            "session_lifecycle": cycle,
        }
        if active:
            result.update(
                work_session_id=f"ws-{self.local_node_id}-{slot.logical_agent_id}", session_epoch=1
            )
        return result

    observed_identity = local_identity


class Runtime:
    def __init__(self, store, task_store, now):
        self.store, self.task_store, self.now = store, task_store, now
        self.resolutions = []

    def clock(self):
        return self.now

    async def observe_intent_call(self, actor, *, require_intent):
        # This transport-only Runtime supplies no intent persistence. Role-wire
        # chunk tests use safe reads; real write/intent admission has own fixtures.
        assert not require_intent
        return {}

    def intent_call_context(self, status):
        return nullcontext()

    def connection_key(self, actor):
        return f"{self.store.local_node_id}/{actor.principal_id}/{actor.endpoint_role}"

    async def resolve(self, actor, operation):
        self.resolutions.append(operation)
        agent_id = actor.principal_id
        if agent_id not in self.store.attached:
            raise MeshMessagingError("session_attach_required")
        identity = self.store.local_identity(self.store.slots[agent_id], now=self.now)
        if operation != ManagedOperation.MESSAGE_READ and "work_session_id" not in identity:
            raise MeshMessagingError("session_expired")
        return identity


class Network:
    def __init__(self):
        self.nodes, self.calls = {}, []

    def transport(self, node_id):
        network = self

        class Replication:
            peers = [
                SimpleNamespace(instance_id=n) for n in ("firstbyte", "bacloud") if n != node_id
            ]

            async def request(self, peer, path, payload):
                network.calls.append((node_id, peer.instance_id, path, payload))
                response = await network.nodes[peer.instance_id].handle_peer(
                    node_id, path.rsplit("/", 1)[-1], payload
                )
                return response

        return Replication()


def actor(agent_id, request_id="same-request", role="coordinator"):
    return ActorContext(
        transport="mcp", principal_id=agent_id, request_id=request_id, endpoint_role=role
    )


@pytest_asyncio.fixture
async def mesh_pair(tmp_path):
    now = datetime(2026, 10, 8, 10, tzinfo=UTC)
    slots = {
        name: SimpleNamespace(
            logical_agent_id=name,
            issuer_id=issuer,
            slot_id=name,
            active=True,
            started_at=now - timedelta(seconds=30),
            expires_at=now + timedelta(minutes=15)
            if name != "expired"
            else now - timedelta(seconds=1),
        )
        for name, issuer in (
            ("alice", "firstbyte"),
            ("bob", "bacloud"),
            ("carol", "firstbyte"),
            ("expired", "bacloud"),
        )
    }
    network = Network()
    for node_id, attached in (
        ("firstbyte", {"alice", "carol"}),
        ("bacloud", {"alice", "bob", "carol", "expired"}),
    ):
        repo = SqliteRepository(tmp_path / f"{node_id}.db", tmp_path / f"{node_id}-output.db")
        await repo.initialize()
        agents = AgentStore(repo.path)
        runtime = Runtime(LocalSlots(node_id, slots, attached), TaskStore(repo.path), now)
        messages = AccessMeshMessaging(runtime, agents, network.transport(node_id))
        with messages.store.connect() as db:
            db.executescript("""
                CREATE TABLE access_mesh_activity(
                    connection_key TEXT PRIMARY KEY, last_active_at TEXT);
                CREATE TABLE access_mesh_attachments(
                    connection_key TEXT PRIMARY KEY, issuer_id TEXT,
                    slot_id TEXT, logical_agent_id TEXT);
            """)
            for agent_id in attached:
                slot = slots[agent_id]
                db.execute(
                    "INSERT INTO access_mesh_activity VALUES(?,?)", (agent_id, utc_text(now))
                )
                db.execute(
                    "INSERT INTO access_mesh_attachments VALUES(?,?,?,?)",
                    (agent_id, slot.issuer_id, slot.slot_id, agent_id),
                )
        network.nodes[node_id] = messages
    yield network
    for node in network.nodes.values():
        await node.stop()


def count(node, table, where="1=1"):
    with node.store.connect() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


@pytest.mark.asyncio
async def test_local_broadcast_never_calls_peer_and_excludes_sender(mesh_pair):
    net = mesh_pair
    fb, bac = net.nodes["firstbyte"], net.nodes["bacloud"]
    result = await fb.message(actor("alice"), text="local", scope="local")
    assert result["ok"] and result["scope"] == "local"
    assert result["delivered_to"] == [_public_name(fb.mesh.store.slots["carol"])]
    assert not net.calls and count(bac, "coordination_messages") == 0
    assert count(fb, "coordination_message_recipients") == 1
    assert not fb.store.pending()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,destination,sender,recipient,role",
    [
        ("firstbyte", "bacloud", "alice", "bob", "executor"),
    ],
)
async def test_remote_only_recipient_is_rejected_before_local_mutation(
    mesh_pair, source, destination, sender, recipient, role
):
    net = mesh_pair
    origin, remote = net.nodes[source], net.nodes[destination]
    origin.mesh.store.attached.discard(recipient)
    target = _public_name(origin.mesh.store.slots[recipient])
    sent = await origin.message(
        actor(sender, role=role), text="remote-only recipient", target=target, alert=True
    )
    assert sent["ok"] is False and sent["code"] == "recipient_not_found"
    assert count(origin, "coordination_messages") == 0
    assert count(remote, "coordination_messages") == 0
    assert not net.calls


@pytest.mark.asyncio
async def test_keyset_pagination_does_not_skip_consumed_inbox_rows(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    for n in range(5):
        result = await fb.message(actor("alice", str(n)), text=f"message {n}", scope="local")
        assert result["ok"]
    seen, cursor = [], None
    while True:
        page = await fb.message(actor("carol"), limit=2, cursor=cursor)
        assert page["ok"], page
        seen.extend(row["message_hash"] for row in page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(set(seen)) == len(seen) == 5
    assert (await fb.message(actor("carol")))["messages"] == []
    assert len((await fb.message(actor("carol"), history=True))["messages"]) == 5


@pytest.mark.asyncio
async def test_recipient_discovery_is_unique_active_scoped_and_public(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    result = await fb.message(actor("alice"), recipients=True, limit=1)
    assert result["ok"] and result["next_cursor"] is None
    assert len(result["recipients"]) == 1
    recipient = result["recipients"][0]
    assert recipient["public_name"] == _public_name(fb.mesh.store.slots["carol"])
    assert recipient["remaining_seconds"] == 900
    assert _public_name(fb.mesh.store.slots["bob"]) not in {
        item["public_name"] for item in result["recipients"]
    }
    invalid = await fb.message(actor("carol"), recipients=True, cursor="not-a-cursor")
    assert not invalid["ok"] and invalid["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_reply_failure_is_atomic_and_retry_releases_obligation_once(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(actor("alice"), text="must reply", scope="local", alert=True)
    message_hash = sent["message_hash"]
    with fb.store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_reply BEFORE INSERT ON access_mesh_messages "
            "WHEN json_extract(NEW.wire_json,'$.reply_to') IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT,'injected reply commit failure'); END"
        )
    with pytest.raises(Exception, match="injected reply commit failure"):
        await fb.message(actor("carol"), text="reply", message_hash=message_hash)
    assert count(fb, "coordination_messages") == 1
    assert len(await fb.agent_store.message_obligations("carol")) == 1
    with fb.store.connect() as db:
        db.execute("DROP TRIGGER reject_reply")
    replied = await fb.message(actor("carol"), text="reply", message_hash=message_hash)
    again = await fb.message(actor("carol", "another-id"), text="reply", message_hash=message_hash)
    assert replied["ok"] and again["ok"]
    assert again["message_hash"] != replied["message_hash"]
    assert count(fb, "coordination_messages") == 3
    assert not await fb.agent_store.message_obligations("carol")


@pytest.mark.asyncio
async def test_response_preflight_failure_does_not_acknowledge(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(actor("alice"), text="large\\" * 2200, scope="local")
    assert sent["ok"], sent
    result = await fb.message(
        actor("carol"),
        detail="full",
        response_preflight=lambda _: {"ok": False, "code": "output_item_too_large"},
    )
    assert result["ok"] is False
    row = await fb.agent_store.recipient_record(sent["message_hash"], "carol")
    assert row["read_at"] is None and row["seen_count"] == 0
    result = await fb.message(actor("carol"))
    assert result["ok"] and result["messages"][0]["truncated"]


def test_http_router_checks_peer_auth_and_request_size():
    class Messages:
        peers = [SimpleNamespace(instance_id="firstbyte")]

        async def handle_peer(self, peer_id, kind, payload):
            return {"ok": False, "peer": peer_id, "code": "feature_disabled",
                    "outcome": "not_committed"}

        async def handle_locations_peer(self, peer_id, payload):
            return {"ok": True, "peer": peer_id, "kind": "locations", "payload": payload}

    auth = SimpleNamespace(
        authenticate=lambda peer, token: (
            SimpleNamespace(instance_id=peer)
            if peer == "firstbyte" and token == "Bearer test"
            else None
        )
    )
    app = FastAPI()
    app.include_router(build_access_mesh_message_router(Messages(), auth))
    with TestClient(app) as client:
        path = "/internal/fleet/access-mesh/messages/recipients"
        assert client.post(path, json={}).status_code == 401
        headers = {"x-terminal-mcp-peer": "firstbyte", "authorization": "Bearer test"}
        response = client.post(path, json={"limit": 10}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": False, "peer": "firstbyte", "code": "feature_disabled",
            "outcome": "not_committed",
        }
        assert client.post(path, content="x" * 70000, headers=headers).status_code == 413
        assert client.post(path, content="[]", headers=headers).status_code == 400

        presence_path = "/internal/fleet/access-mesh/agents/locations"
        assert client.post(presence_path, json={}).status_code == 401
        presence = client.post(presence_path, json={"limit": 10}, headers=headers)
        assert presence.status_code == 200, presence.text
        assert presence.json()["kind"] == "locations"


@pytest.mark.asyncio
async def test_missing_local_target_is_a_structured_noncommitted_failure(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    result = await fb.message(
        actor("alice"),
        text="no local target",
        scope="local",
        target=_public_name(fb.mesh.store.slots["bob"]),
    )
    assert result["code"] == "recipient_not_found" and result["outcome"] == "not_committed"
    assert count(fb, "coordination_messages") == 0


@pytest.mark.asyncio
async def test_reply_requirement_cannot_be_weakened_by_explicit_notify_mode(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    result = await fb.message(
        actor("alice"), text="reply required", scope="local", mode="notify", require_reply=True
    )
    assert result["ok"] and result["mode"] == "alert"
    await fb.message(actor("carol"))
    await fb.message(actor("carol"), message_hash=result["message_hash"])
    obligations = await fb.agent_store.message_obligations("carol")
    assert len(obligations) == 1 and obligations[0]["replied_at"] is None


@pytest.mark.asyncio
async def test_postcommit_readback_failure_does_not_hide_local_acceptance(mesh_pair, monkeypatch):
    fb = mesh_pair.nodes["firstbyte"]

    def fail(*args):
        raise RuntimeError("readback unavailable")

    monkeypatch.setattr(fb.store, "receipt", fail)
    result = await fb.message(actor("alice"), text="committed", scope="local")
    assert result["ok"] and result["outcome"] == "committed"
    assert fb.store.wire(result["message_hash"])["text"] == "committed"
    assert count(fb, "coordination_message_recipients") == 1


@pytest.mark.asyncio
async def test_oversized_full_item_stays_unread_and_summary_is_bounded(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(actor("alice"), text="\\" * 16000, scope="local")
    assert sent["ok"]
    full = await fb.message(actor("carol"), detail="full")
    assert full["ok"] is False and full["code"] == "output_item_too_large"
    row = await fb.agent_store.recipient_record(sent["message_hash"], "carol")
    assert row["read_at"] is None and row["seen_count"] == 0
    summary = await fb.message(actor("carol"))
    assert summary["ok"] and summary["messages"][0]["truncated"]


@pytest.mark.asyncio
async def test_history_cursor_is_stable_after_retention_and_vacuum(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = []
    for n in range(5):
        sent.append(
            (await fb.message(actor("alice", str(n)), text=str(n), scope="local"))["message_hash"]
        )
    first = await fb.message(actor("carol"), history=True, limit=2)
    with fb.store.connect() as db:
        db.execute("DELETE FROM coordination_messages WHERE message_hash=?", (sent[0],))
    with fb.store.connect() as db:
        db.execute("VACUUM")
    second = await fb.message(actor("carol"), history=True, limit=2, cursor=first["next_cursor"])
    assert second["ok"]
    assert {row["message_hash"] for row in first["messages"] + second["messages"]} == set(sent[1:])


@pytest.mark.asyncio
async def test_recipient_read_never_materializes_or_updates_work_session(mesh_pair, monkeypatch):
    fb = mesh_pair.nodes["firstbyte"]
    identity = fb.mesh.store.observed_identity(fb.mesh.store.slots["alice"], now=fb.mesh.clock())

    def reject_write(*args, **kwargs):
        raise AssertionError("A recipient read attempted lifecycle materialization")

    monkeypatch.setattr(fb.mesh.store, "local_identity", reject_write)
    result = await fb.recipients(identity, scope="local", limit=20, cursor=None)
    assert result["ok"] and len(result["recipients"]) == 1


@pytest.mark.asyncio
async def test_directed_self_send_has_distinct_error_and_no_side_effect(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sender_name = _public_name(fb.mesh.store.slots["alice"])
    before = count(fb, "coordination_messages")
    response = await fb.message(
        actor("alice"),
        text="must never be inserted",
        scope="local",
        target=sender_name,
    )
    assert response == {
        "ok": False,
        "code": "cannot_message_self",
        "error": "Нельзя отправить сообщение самому себе",
        "outcome": "not_committed",
    }
    assert count(fb, "coordination_messages") == before

"""Two independent local databases; no route cache or issuer read service."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.application.access_mesh_messages import _public_name as _public_name
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.access_mesh_messages import MeshMessagingError
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class LocalSlots:
    def __init__(self, node_id, slots, attached):
        self.local_node_id, self.slots, self.attached = node_id, slots, attached
        self.namespace_id = "standalone-" + node_id
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
async def test_local_broadcast_delivers_to_active_local_recipient(mesh_pair):
    node = mesh_pair.nodes["firstbyte"]
    result = await node.message(actor("alice"), text="local", scope="local")
    assert result["ok"]
    assert result["delivered_to"] == [_public_name(node.mesh.store.slots["carol"])]
    assert count(node, "coordination_message_recipients") == 1


@pytest.mark.asyncio
async def test_inbox_cursor_resumes_after_consumed_page(mesh_pair):
    node = mesh_pair.nodes["firstbyte"]
    sent = [
        (await node.message(actor("alice", str(n)), text=f"message {n}"))["message_hash"]
        for n in range(3)
    ]
    first = await node.message(actor("carol"), limit=1)
    second = await node.message(actor("carol"), limit=2, cursor=first["next_cursor"])
    assert second["ok"]
    assert {row["message_hash"] for row in second["messages"]} == set(sent[:2])
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_recipient_discovery_is_unique_active_scoped_and_public(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    result = await fb.message(actor("alice"), recipients=True, limit=1)
    assert result["ok"] and result["next_cursor"] is None
    assert len(result["recipients"]) == 1
    recipient = result["recipients"][0]
    assert recipient["public_name"] == _public_name(fb.mesh.store.slots["carol"])
    assert recipient["remaining_seconds"] == 900


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
async def test_history_cursor_survives_retention_of_an_earlier_row(mesh_pair):
    node = mesh_pair.nodes["firstbyte"]
    sent = [
        (await node.message(actor("alice", str(n)), text=str(n)))["message_hash"] for n in range(3)
    ]
    first = await node.message(actor("carol"), history=True, limit=1)
    with node.store.connect() as db:
        db.execute("DELETE FROM coordination_messages WHERE message_hash=?", (sent[0],))
    with node.store.connect() as db:
        db.execute("VACUUM")
    second = await node.message(actor("carol"), history=True, limit=1, cursor=first["next_cursor"])
    assert second["ok"]
    assert [row["message_hash"] for row in second["messages"]] == [sent[1]]


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

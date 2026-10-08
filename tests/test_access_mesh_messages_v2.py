"""Two independent local databases; no route cache or issuer read service."""

import asyncio
import time
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

    def local_slots(self, *, after="", limit=50):
        return sorted(
            (
                s
                for s in self.slots.values()
                if s.logical_agent_id in self.attached and s.logical_agent_id > after
            ),
            key=lambda s: s.logical_agent_id,
        )[:limit]

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
        self.nodes, self.offline, self.calls = {}, set(), []
        self.drop_ack_once, self.before_request = False, None

    def transport(self, node_id):
        network = self

        class Replication:
            peers = [
                SimpleNamespace(instance_id=n) for n in ("firstbyte", "bacloud") if n != node_id
            ]

            async def request(self, peer, path, payload):
                network.calls.append((node_id, peer.instance_id, path, payload))
                if network.before_request:
                    network.before_request(node_id, peer.instance_id, path, payload)
                if peer.instance_id in network.offline:
                    raise ConnectionError("peer unavailable")
                response = await network.nodes[peer.instance_id].handle_peer(
                    node_id, path.rsplit("/", 1)[-1], payload
                )
                if network.drop_ack_once and path.endswith("deliver"):
                    network.drop_ack_once = False
                    raise ConnectionError("delivered but HTTP ACK lost")
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
async def test_fleet_commits_local_first_deduplicates_and_preserves_identity(mesh_pair):
    net = mesh_pair
    fb, bac = net.nodes["firstbyte"], net.nodes["bacloud"]

    def local_first(source, peer, path, payload):
        if source == "firstbyte" and path.endswith("deliver"):
            assert fb.store.wire(payload["message"]["message_hash"])
            assert count(fb, "coordination_message_recipients") == 1

    net.before_request = local_first
    result = await fb.message(actor("alice"), text="fleet", target="broadcast")
    assert result["ok"] and result["state"] == "delivered", result
    assert result["recipient_count"] == 2
    assert count(fb, "coordination_message_recipients") == 1
    assert count(bac, "coordination_message_recipients") == 1
    assert not await bac.agent_store.message_journal("carol")
    incoming = await bac.message(actor("bob", role="executor"))
    assert len(incoming["messages"]) == 1
    assert incoming["messages"][0]["sender"] == _public_name(fb.mesh.store.slots["alice"])
    assert incoming["messages"][0]["message_hash"] == result["message_hash"]
    assert not await bac.agent_store.message_journal("expired")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,destination,sender,recipient",
    [
        ("firstbyte", "bacloud", "alice", "bob"),
        ("bacloud", "firstbyte", "bob", "alice"),
    ],
)
async def test_directed_agent_attached_on_both_nodes_gets_each_local_inbox(
    mesh_pair, source, destination, sender, recipient
):
    """Peer excluded=broadcast dedup must not suppress directed delivery."""
    net = mesh_pair
    origin, remote = net.nodes[source], net.nodes[destination]
    # Bob is normally attached only on BacLOUD. Simulate dual attachment,
    # just as both production role consumers attach the same issuer slot.
    origin.mesh.store.attached.add(recipient)
    assert recipient in remote.mesh.store.attached
    target = _public_name(origin.mesh.store.slots[recipient])

    receipt = await origin.message(
        actor(sender, role="executor"),
        text="deliver to both local inboxes",
        target=target,
        scope="fleet",
    )
    assert receipt["ok"] is True, receipt
    assert receipt["state"] == "delivered"
    # The delivery receipt counts distinct LogicalAgents globally (one),
    # while both node-local inboxes must persist the directed message.
    assert receipt["recipient_count"] == 1
    msg_hash = receipt["message_hash"]
    assert count(origin, "coordination_message_recipients") == 1
    assert count(remote, "coordination_message_recipients") == 1
    local = await origin.message(actor(recipient, role="coordinator"))
    on_remote = await remote.message(actor(recipient, role="coordinator"))
    assert any(row["message_hash"] == msg_hash for row in local["messages"])
    assert any(row["message_hash"] == msg_hash for row in on_remote["messages"])

    # Lost-ACK retry must not insert duplicates into either local inbox.
    response = await origin.flush(message_hash=msg_hash)
    assert response is None
    assert count(origin, "coordination_message_recipients") == 1
    assert count(remote, "coordination_message_recipients") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,destination,sender,recipient,role",
    [
        ("firstbyte", "bacloud", "alice", "bob", "executor"),
        ("bacloud", "firstbyte", "bob", "alice", "coordinator"),
    ],
)
async def test_first_contact_send_read_ack_reply_history(
    mesh_pair, source, destination, sender, recipient, role
):
    net = mesh_pair
    origin, remote = net.nodes[source], net.nodes[destination]
    origin.mesh.store.attached.discard(recipient)
    target = _public_name(origin.mesh.store.slots[recipient])
    sent = await origin.message(
        actor(sender, role=role), text="need reply", target=target, alert=True
    )
    assert sent["ok"] and sent["state"] == "delivered", sent
    message_hash = sent["message_hash"]
    obligations = await remote.agent_store.message_obligations(recipient)
    assert len(obligations) == 1 and obligations[0]["alert"]
    before_calls = len(net.calls)
    read = await remote.message(actor(recipient), detail="full")
    assert read["ok"] and len(read["messages"]) == 1
    assert len(net.calls) == before_calls
    ack = await remote.message(actor(recipient), message_hash=message_hash)
    assert ack["ok"] and len(net.calls) == before_calls
    assert len(await remote.agent_store.message_obligations(recipient)) == 1
    replied = await remote.message(actor(recipient), message_hash=message_hash, text="resolved")
    assert replied["ok"] and replied["reply_to"] == message_hash, replied
    assert await remote.agent_store.message_obligations(recipient) == []
    await remote.tick()
    assert origin.store.receipt(message_hash)["deliveries"][0]["state"] == "replied"
    history = await remote.message(actor(recipient), history=True, detail="full")
    assert {message_hash, replied["message_hash"]} <= {
        row["message_hash"] for row in history["messages"]
    }
    assert any(
        row["message_hash"] == replied["message_hash"]
        for row in (await origin.message(actor(sender), history=True))["messages"]
    )


@pytest.mark.asyncio
async def test_offline_peer_retains_local_success_and_outbox_survives_restart(mesh_pair):
    net, fb = mesh_pair, mesh_pair.nodes["firstbyte"]
    net.offline.add("bacloud")
    result = await fb.message(actor("alice"), text="survives partition")
    assert result["ok"] and result["outcome"] == "committed"
    assert result["state"] == "partial" and result["pending_peers"] == ["bacloud"]
    assert result["delivery_errors"][0]["code"] == "message_unavailable"
    restarted = AccessMeshMessaging(fb.mesh, fb.agent_store, net.transport("firstbyte"))
    net.nodes["firstbyte"] = restarted
    assert len(restarted.store.pending()) == 1
    net.offline.clear()
    await restarted.tick()
    assert restarted.store.receipt(result["message_hash"])["state"] == "delivered"
    assert not restarted.store.pending()
    assert len(await net.nodes["bacloud"].agent_store.message_journal("bob")) == 1


@pytest.mark.asyncio
async def test_lost_ack_duplicate_delivery_and_same_request_have_one_effect(mesh_pair):
    net = mesh_pair
    fb, bac = net.nodes["firstbyte"], net.nodes["bacloud"]
    net.drop_ack_once = True
    first = await fb.message(actor("alice"), text="one message")
    assert first["state"] == "partial" and count(bac, "coordination_messages") == 1
    second = await fb.message(actor("alice"), text="one message")
    assert first["message_hash"] == second["message_hash"]
    assert second["state"] == "delivered"
    assert count(fb, "coordination_messages") == count(bac, "coordination_messages") == 1
    assert count(bac, "coordination_message_recipients") == 1
    changed = await fb.message(actor("alice"), text="different message")
    assert changed["message_hash"] != first["message_hash"]
    assert count(bac, "coordination_messages") == 2


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
    first = await fb.message(actor("alice"), recipients=True, limit=1)
    assert first["ok"] and first["next_cursor"]
    assert len(first["recipients"]) == 1
    recipient = first["recipients"][0]
    assert set(recipient) == {
        "public_name",
        "session_state",
        "session_started_at",
        "hard_expires_at",
        "remaining_seconds",
        "last_active_at",
    }
    assert recipient["remaining_seconds"] == 900
    second = await fb.message(actor("alice"), recipients=True, limit=1, cursor=first["next_cursor"])
    assert second["ok"] and second["next_cursor"] is None
    assert second["recipients"][0]["public_name"] != recipient["public_name"]
    invalid = await fb.message(actor("carol"), recipients=True, cursor=first["next_cursor"])
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
    assert replied["ok"]
    again = await fb.message(actor("carol", "another-id"), text="reply", message_hash=message_hash)
    assert again["message_hash"] == replied["message_hash"]
    assert count(fb, "coordination_messages") == 2
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


@pytest.mark.asyncio
async def test_attribution_receipts_and_peer_auth_reject_forgery(mesh_pair):
    fb, bac = mesh_pair.nodes["firstbyte"], mesh_pair.nodes["bacloud"]
    sent = await fb.message(actor("alice"), text="original")
    wire = fb.store.wire(sent["message_hash"])
    result = await bac.handle_peer("untrusted", "deliver", {"message": wire, "excluded": []})
    assert result["ok"] is False
    tampered = {**wire, "sender_name": "forged"}
    result = await bac.handle_peer("firstbyte", "deliver", {"message": tampered, "excluded": []})
    assert result["code"] == "sender_not_authorized"
    wrong = await bac.message(actor("alice"), message_hash=sent["message_hash"])
    assert wrong["code"] == "message_forbidden"
    expired = await bac.message(actor("expired"), text="cannot send")
    assert expired["code"] == "session_expired"


def test_http_router_checks_peer_auth_and_request_size():
    class Messages:
        peers = [SimpleNamespace(instance_id="firstbyte")]

        async def handle_peer(self, peer_id, kind, payload):
            return {"ok": True, "peer": peer_id, "kind": kind, "payload": payload}

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
        assert response.json()["peer"] == "firstbyte"
        assert client.post(path, content="x" * 70000, headers=headers).status_code == 413
        assert client.post(path, content="[]", headers=headers).status_code == 400


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
@pytest.mark.parametrize("occupied_lock", [False, True])
async def test_public_send_returns_durable_receipt_within_total_peer_budget(
    mesh_pair, monkeypatch, occupied_lock
):
    fb = mesh_pair.nodes["firstbyte"]
    monkeypatch.setattr(
        "terminal_mcp.application.access_mesh_messages.PUBLIC_DELIVERY_BUDGET_SECONDS", 0.05
    )

    async def offline(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(fb.replication, "request", offline)
    if occupied_lock:
        await fb._flush_lock.acquire()
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            fb.message(actor("alice"), text="accepted while peer is stalled"), timeout=0.8
        )
    finally:
        if occupied_lock:
            fb._flush_lock.release()
    assert time.monotonic() - started < 0.8
    assert result["ok"] and result["outcome"] == "committed"
    assert result["state"] == "partial" and result["pending_peers"] == ["bacloud"]
    assert fb.store.wire(result["message_hash"])
    assert len(fb.store.pending()) == 1
    assert count(fb, "coordination_message_recipients") == 1


@pytest.mark.asyncio
async def test_large_fleet_envelope_is_rejected_before_local_or_peer_commit(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    template = fb.mesh.store.slots["carol"]
    for index in range(160):
        agent_id = f"recipient-{index}-" + "x" * 135
        fb.mesh.store.slots[agent_id] = SimpleNamespace(
            **{**vars(template), "logical_agent_id": agent_id, "slot_id": agent_id}
        )
        fb.mesh.store.attached.add(agent_id)
    result = await fb.message(actor("alice"), text="\U0001f600" * 12000, scope="fleet")
    assert result["ok"] is False and result["code"] == "output_item_too_large", result
    assert result["outcome"] == "not_committed"
    assert count(fb, "coordination_messages") == 0
    assert count(fb, "coordination_message_recipients") == 0
    assert not fb.store.pending()
    assert not mesh_pair.calls


@pytest.mark.asyncio
async def test_oversized_peer_acceptance_proof_rolls_back_all_obligations(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(actor("alice"), text="template", scope="local")
    wire = {**fb.store.wire(sent["message_hash"]), "message_hash": "firstbyte:meshmsg:large-proof"}
    recipients = [
        {
            "logical_agent_id": f"{index}:" + "a" * 150,
            "public_name": "\u754c" * 40 + f"-{index:012d}",
        }
        for index in range(256)
    ]
    before_messages = count(fb, "coordination_messages")
    before_recipients = count(fb, "coordination_message_recipients")
    with pytest.raises(MeshMessagingError, match="output_item_too_large"):
        fb.store.accept(wire, recipients)
    assert count(fb, "coordination_messages") == before_messages
    assert count(fb, "coordination_message_recipients") == before_recipients
    assert fb.store.wire(wire["message_hash"]) is None

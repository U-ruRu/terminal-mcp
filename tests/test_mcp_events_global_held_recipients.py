"""Trusted subscription holds confer message-only presence across the existing Mesh."""
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_mcp_events_fleet_lifecycle import (
    KEY,
    Routes,
    allowed,
    message_subscription,
    node,
    records,
)

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.session_gate import ManagedOperation
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.access_mesh_messages import MeshMessagingError
from terminal_mcp.storage.agents import AgentStore


def actor(subject, role="access"):
    return ActorContext(
        auth_mode="bearer", principal_id="fixture-user", credential_id="fixture",
        auth_generation=1, scopes=frozenset({"terminal:read", "terminal:execute"}),
        provider="openai",
        provider_metadata={"openai/subject": subject, "openai/session": subject},
        transport="mcp", endpoint_role=role,
    )


async def pair(tmp_path, client, routes):
    source = await node(tmp_path, "source", ("source", "access"), routes, client)
    access = await node(tmp_path, "access", ("source", "access"), routes, client)
    owner = actor("root")
    recipient = await access.mesh.issue(owner, code="1234")
    await access.replication.tick()
    await message_subscription(access, recipient)
    await access.replication.tick()
    peer = source.replication.peers[0]
    await source.replication.sync_numbers(peer)
    await source.replication.sync_recipient_holds(peer)
    assert source.mesh.store.recipient_holds.local_recipients() == [{
        "recipient_agent_id": recipient["logical_agent_id"], "issuer_id": "access",
        "slot_id": recipient["slot_id"], "number": "1234",
    }]
    sender_owner = actor("sender")
    await source.mesh.issue(sender_owner, code="5678")
    sender_actor = replace(sender_owner, endpoint_role="coordinator")
    await source.mesh.attach(sender_actor, session_number="5678")
    for n in (source, access):
        n.mesh.messages = AccessMeshMessaging(n.mesh, AgentStore(n.path), n.replication)
    return source, access, owner, recipient, sender_actor


def root_local_state(source, recipient):
    with sqlite3.connect(source.path) as db:
        return (
            db.execute(
                "SELECT COUNT(*) FROM access_mesh_attachments WHERE logical_agent_id=?",
                (recipient["logical_agent_id"],),
            ).fetchone()[0],
            db.execute(
                "SELECT COUNT(*) FROM access_mesh_local_sessions WHERE issuer_id=? AND slot_id=?",
                ("access", recipient["slot_id"]),
            ).fetchone()[0],
        )


@pytest.mark.asyncio
async def test_remote_hold_no_attachment_discover_send_callback_and_source_ack(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, access, owner, recipient, sender_actor = await pair(tmp_path, client, routes)
        await access.mesh.issuer_session(owner, action="end", code="1234")
        await access.replication.tick()
        assert root_local_state(source, recipient) == (0, 0)
        name = source.mesh.store.human_name(
            source.mesh.store.slot("access", recipient["slot_id"])
        )
        discovery = await source.mesh.messages.message(sender_actor, recipients=True)
        assert discovery["ok"]
        selected = [r for r in discovery["recipients"] if r["public_name"] == name]
        assert len(selected) == 1 and selected[0]["session_state"] == "inactive"
        restarted = AccessMeshStore(
            source.path, local_node_id="source",
            trusted_issuers=frozenset({"source", "access"}), proof_key=KEY,
        )
        assert restarted.recipient_holds.local_recipients() == (
            source.mesh.store.recipient_holds.local_recipients()
        )
        before = root_local_state(source, recipient)
        sent = await source.mesh.messages.message(
            sender_actor, target=name, text="remote held recipient", mode="ack"
        )
        assert sent["ok"] and sent["outcome"] == "committed", sent
        assert before == root_local_state(source, recipient) == (0, 0)
        with pytest.raises(AccessMeshError, match="session_attach_required"):
            await source.mesh.resolve(actor("root", "coordinator"), ManagedOperation.MESSAGE_SEND)
        await source.events.pump()
        await source.replication.tick()
        await access.events.pump()
        seen = []

        async def callback(url, body, headers):
            seen.append(json.loads(body))
            return 200, b"{}"

        adapter = MCPEventsAdapter(None, access.events, sender=callback, access_checker=allowed)
        await adapter.deliver_due()
        assert len(seen) == 1 and seen[0]["name"] == "message.received", seen
        assert seen[0]["data"]["message_id"] == sent["message_hash"]
        assert seen[0]["data"]["server"] == "source"
        assert seen[0]["data"]["text"] == "remote held recipient"
        assert root_local_state(source, recipient) == (0, 0)
        # Delivery is not an ACK; the authoritative inbox and obligation stay on source.
        with sqlite3.connect(source.path) as db:
            state = db.execute(
                "SELECT read_at,replied_at FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (sent["message_hash"], recipient["logical_agent_id"]),
            ).fetchone()
        assert state == (None, None)
        with pytest.raises(MeshMessagingError, match="message_not_found"):
            access.mesh.messages.store.acknowledge(
                sent["message_hash"], recipient["logical_agent_id"], datetime.now(UTC).isoformat()
            )
        assert not [r for r in records(access) if r[1] == "message.received" and
                    json.loads(r[3])["message_id"] != sent["message_hash"]]
        await access.events.unsubscribe("private", "fixture-user")
        await access.replication.tick()
        await source.replication.sync_recipient_holds(source.replication.peers[0])
        removed = await source.mesh.messages.message(sender_actor, recipients=True)
        assert not [r for r in removed["recipients"] if r["public_name"] == name]
        denied = await source.mesh.messages.message(
            sender_actor, target=name, text="after unsubscribe"
        )
        assert not denied["ok"] and denied["code"] == "recipient_not_found", denied
        assert root_local_state(source, recipient) == (0, 0)
        with sqlite3.connect(source.path) as db:
            assert db.execute(
                "SELECT read_at,replied_at FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (sent["message_hash"], recipient["logical_agent_id"]),
            ).fetchone() == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "withdrawn", "untrusted_source", "untrusted_issuer", "non_issuer_without_attachment",
    "wrong_agent", "wrong_number", "suspended", "deleted", "superseded_winner",
])
async def test_remote_hold_rejects_invalid_current_tuple(tmp_path, invalid):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, access, owner, recipient, sender_actor = await pair(tmp_path, client, routes)
        if invalid in {"untrusted_source", "untrusted_issuer"}:
            source.mesh.store.trusted_issuers = frozenset({"source"})
        else:
            with sqlite3.connect(source.path) as db:
                if invalid == "withdrawn":
                    db.execute("UPDATE access_mesh_recipient_holds SET state='released'")
                elif invalid == "non_issuer_without_attachment":
                    db.execute(
                        "UPDATE access_mesh_recipient_holds SET source_node_id='source-other'"
                    )
                    source.mesh.store.trusted_issuers |= {"source-other"}
                elif invalid == "wrong_agent":
                    db.execute(
                        "UPDATE access_mesh_recipient_holds SET recipient_agent_id='different'"
                    )
                elif invalid == "wrong_number":
                    db.execute("UPDATE access_mesh_recipient_holds SET number='9999'")
                elif invalid in {"suspended", "deleted"}:
                    db.execute(
                        "UPDATE access_mesh_slot_replicas SET state=? WHERE issuer_id='access'",
                        (invalid,),
                    )
        if invalid == "superseded_winner":
            now = datetime.now(UTC)
            # New collisions are pin-blocked. Model a restored historical claim
            # predating the hold, rather than weakening the normal admission rule.
            with pytest.raises(AccessMeshError, match="access_mesh_subscription_hold"):
                source.mesh.store.numbers.register(
                    number="1234", issuer_id="source", slot_id="later",
                    logical_agent_id="different", started_at=now,
                    hard_expires_at=now + timedelta(hours=1),
                )
            with sqlite3.connect(source.path) as db:
                db.execute(
                    "INSERT INTO access_mesh_number_claims VALUES(?,?,?,?,?,?)",
                    ("source", "later", "1234", "different", now.isoformat(),
                     (now + timedelta(hours=1)).isoformat()),
                )
        assert not [r for r in source.mesh.store.recipient_holds.local_recipients()
                    if r["recipient_agent_id"] == recipient["logical_agent_id"]]
        name = source.mesh.store.human_name(
            source.mesh.store.slot("access", recipient["slot_id"])
        )
        result = await source.mesh.messages.message(sender_actor, recipients=True)
        assert result["ok"] and not [r for r in result["recipients"]
                                     if r["public_name"] == name], result
        sent = await source.mesh.messages.message(
            sender_actor, target=name, text="must not commit"
        )
        assert not sent["ok"] and sent["code"] == "recipient_not_found", sent


@pytest.mark.asyncio
async def test_expired_finite_subscription_pins_global_presence_but_stops_delivery(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        source, access, owner, recipient, sender_actor = await pair(tmp_path, client, routes)
        await access.mesh.issuer_session(owner, action="end", code="1234")
        await access.replication.tick()
        with sqlite3.connect(access.path) as db:
            db.execute(
                "UPDATE mcp_event_subscriptions SET expires_at=? WHERE id='private'",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        await access.replication.tick()
        await source.replication.sync_recipient_holds(source.replication.peers[0])
        assert source.mesh.store.recipient_holds.local_recipients()
        name = source.mesh.store.human_name(
            source.mesh.store.slot("access", recipient["slot_id"])
        )
        discovery = await source.mesh.messages.message(sender_actor, recipients=True)
        assert any(r["public_name"] == name for r in discovery["recipients"])
        sent = await source.mesh.messages.message(
            sender_actor, target=name, text="finite expiry preserves inbox", mode="ack"
        )
        assert sent["ok"] and sent["outcome"] == "committed", sent
        await source.events.pump()
        await source.replication.tick()
        await access.events.pump()
        seen = []

        async def callback(url, body, headers):
            seen.append(json.loads(body))
            return 200, b"{}"

        await MCPEventsAdapter(
            None, access.events, sender=callback, access_checker=allowed
        ).deliver_due()
        assert seen == []
        assert root_local_state(source, recipient) == (0, 0)
        restarted = AccessMeshStore(
            source.path, local_node_id="source",
            trusted_issuers=frozenset({"source", "access"}), proof_key=KEY,
        )
        assert restarted.recipient_holds.local_recipients()
        with sqlite3.connect(source.path) as db:
            assert db.execute(
                "SELECT read_at,replied_at FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (sent["message_hash"], recipient["logical_agent_id"]),
            ).fetchone() == (None, None)

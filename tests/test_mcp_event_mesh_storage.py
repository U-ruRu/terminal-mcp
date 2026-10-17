"""Current event replication boundaries with one database and explicit peer input."""

import base64
import hashlib
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from test_mcp_event_subscriptions import NODE, record, subscription
from test_mcp_event_subscriptions import events as events

from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.fleet.event_mesh import LifecycleEventMesh, build_lifecycle_event_router
from terminal_mcp.fleet.message_event_mesh import RECIPIENT_PAGE, wire_bytes
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore


def lifecycle(store):
    return LifecycleEventMesh(store, AsyncMock())


def lifecycle_event(*, event_id="evt_" + "a" * 32, server="source", timestamp=None):
    return {
        "eventId": event_id,
        "name": "agent.attached",
        "timestamp": timestamp or utc_text(utc_now() - timedelta(seconds=30)),
        "data": {"server": server, "agent_name": "Peer worker"},
    }


@pytest.mark.asyncio
async def test_lifecycle_import_replay_keeps_one_source_event(events):
    await subscription(events, agent=None, name="agent.attached")
    event = lifecycle_event()
    mesh = lifecycle(events)
    await mesh.receive_events("source", {"events": [event]})

    result = await mesh.receive_events("source", {"events": [event]})

    assert result["acked"] == [event["eventId"]]
    with sqlite3.connect(events.path) as db:
        rows = db.execute(
            "SELECT event_id,origin_node_id,data_json FROM mcp_event_records"
        ).fetchall()
    assert [(row[0], row[1], json.loads(row[2])) for row in rows] == [
        (event["eventId"], "source", event["data"])
    ]


@pytest.mark.asyncio
async def test_lifecycle_batch_rejects_a_forged_source_before_any_import(events):
    await subscription(events, agent=None, name="agent.attached")
    forged = lifecycle_event(event_id="evt_" + "b" * 32, server="different-source")

    with pytest.raises(ValueError, match="invalid lifecycle event data"):
        await lifecycle(events).receive_events("source", {"events": [lifecycle_event(), forged]})

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT COUNT(*) FROM mcp_event_records").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_lifecycle_interest_backfills_only_events_since_subscription_start(events):
    now = utc_now()
    start = utc_text(now - timedelta(seconds=20))
    record(events, subscriptions=(), name="agent.attached")
    current = record(events, "evt_" + "2" * 32, subscriptions=(), name="agent.attached")
    with sqlite3.connect(events.path) as db:
        db.execute("UPDATE mcp_event_records SET occurred_at=? WHERE event_id=?", (start, current))

    await lifecycle(events).receive_interest(
        "source",
        {
            "version": 1,
            "event_names": ["agent.attached"],
            "started_at": {"agent.attached": start},
        },
        now=now,
    )

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT event_id FROM mcp_event_mesh_outbox").fetchall() == [(current,)]


@pytest.mark.asyncio
async def test_stale_lifecycle_interest_cannot_restore_a_withdrawn_subscription(events):
    mesh = lifecycle(events)
    await mesh.receive_interest("source", {"version": 2, "event_names": [], "started_at": {}})

    await mesh.receive_interest(
        "source",
        {
            "version": 1,
            "event_names": ["agent.attached"],
            "started_at": {"agent.attached": utc_text(utc_now() - timedelta(seconds=30))},
        },
    )

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT version FROM mcp_event_mesh_versions").fetchone() == (2,)
        assert db.execute("SELECT name FROM mcp_event_mesh_interest").fetchall() == []


@pytest.mark.asyncio
async def test_interest_renewal_keeps_the_original_backfill_boundary(events):
    mesh = lifecycle(events)
    start = utc_text(utc_now() - timedelta(minutes=1))
    await mesh.receive_interest(
        "source",
        {
            "version": 1,
            "event_names": ["agent.attached"],
            "started_at": {"agent.attached": start},
        },
    )

    await mesh.receive_interest(
        "source",
        {
            "version": 1,
            "event_names": ["agent.attached"],
            "started_at": {"agent.attached": utc_text()},
        },
    )

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT started_at FROM mcp_event_mesh_interest").fetchone() == (start,)


@pytest.mark.asyncio
async def test_store_reopen_recovers_an_interrupted_mesh_delivery(events):
    event_id = record(events, subscriptions=(), name="agent.attached")
    with sqlite3.connect(events.path) as db:
        db.execute(
            "INSERT INTO mcp_event_mesh_outbox(event_id,peer_node_id,stat"
            "e,attempts,next_attempt_at,created_at) "
            "VALUES(?,'source','sending',2,?,?)",
            (event_id, utc_text(), utc_text()),
        )

    await MCPEventStore(events.path, NODE).initialize()

    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT event_id,state,attempts FROM mcp_event_mesh_outbox"
        ).fetchone() == (event_id, "pending", 2)


@pytest.mark.asyncio
async def test_lifecycle_route_rechecks_current_peer_membership():
    receiver = SimpleNamespace(receive_events=AsyncMock())
    auth = SimpleNamespace(
        authenticate=Mock(return_value=SimpleNamespace(instance_id="removed-peer"))
    )
    mesh = SimpleNamespace(store=SimpleNamespace(trusted_issuers=frozenset({NODE})))
    router = build_lifecycle_event_router(receiver, auth, mesh)
    route = next(route for route in router.routes if route.path.endswith("/events"))

    with pytest.raises(HTTPException) as error:
        await route.endpoint({"events": [lifecycle_event()]}, "removed-peer", "Bearer peer-token")

    assert error.value.status_code == 401
    receiver.receive_events.assert_not_awaited()


def private_event():
    timestamp = utc_text(utc_now() - timedelta(seconds=30))
    return {
        "eventId": "evt_" + "c" * 32,
        "name": "message.received",
        "timestamp": timestamp,
        "recipient": {"issuer_id": NODE, "slot_id": "slot", "recipient_agent_id": "recipient"},
        "data": {
            "server": "source",
            "sender": "Sender",
            "recipient": "Recipient",
            "message_id": "source:localmsg:message",
            "text": 'A line\n"quoted"',
            "timestamp": timestamp,
        },
    }


@pytest.mark.asyncio
async def test_private_event_import_preserves_the_source_message_and_recipient(events):
    await subscription(events)
    event = private_event()

    await lifecycle(events).message_events.receive_events("source", {"events": [event]})

    with sqlite3.connect(events.path) as db:
        row = db.execute(
            "SELECT origin_node_id,recipient_agent_id,data_json FROM mcp_event_records"
        ).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == ("source", "recipient", event["data"])


@pytest.mark.asyncio
async def test_private_message_reassembly_uses_the_chunk_saved_before_restart(events):
    await subscription(events)
    event = private_event()
    raw = wire_bytes(event)
    split = len(raw) // 2
    common = {
        "eventId": event["eventId"],
        "timestamp": event["timestamp"],
        "recipient": event["recipient"],
        "digest": hashlib.sha256(raw).hexdigest(),
        "total": 2,
    }
    await lifecycle(events).message_events.receive_chunk(
        "source",
        {
            **common,
            "index": 0,
            "part": base64.b64encode(raw[:split]).decode(),
        },
    )

    result = await lifecycle(events).message_events.receive_chunk(
        "source",
        {
            **common,
            "index": 1,
            "part": base64.b64encode(raw[split:]).decode(),
        },
    )

    assert result == {"ok": True, "complete": True}
    with sqlite3.connect(events.path) as db:
        row = db.execute("SELECT event_id,data_json FROM mcp_event_records").fetchone()
    assert (row[0], json.loads(row[1])) == (event["eventId"], event["data"])


def replica(store, *, count=1):
    mesh = AccessMeshStore(
        store.path,
        local_node_id=NODE,
        trusted_issuers=frozenset({NODE, "source"}),
        proof_key=b"unit-test-mesh-proof-key-32bytes",
    )
    mesh.apply_event(
        AccessSlotEvent(
            issuer_id="source",
            slot_id="slot",
            logical_agent_id="recipient",
            event_id="issued",
            revision=1,
            kind="SlotIssued",
            policy=SlotPolicy(duration_seconds=60, cooldown_seconds=0),
            code_tag=mesh.code_tag("source", "1234"),
            effective_at=utc_now(),
        ),
        authenticated_peer_id="source",
        issued_kind="persistent",
    )
    if count > 1:
        with sqlite3.connect(store.path) as db:
            db.row_factory = sqlite3.Row
            original = dict(db.execute("SELECT * FROM access_mesh_slot_replicas").fetchone())
            columns = list(original)
            query = (
                "INSERT INTO access_mesh_slot_replicas("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")"
            )
            db.executemany(
                query,
                [
                    tuple(
                        {
                            **original,
                            "slot_id": f"slot-{i}",
                            "logical_agent_id": f"recipient-{i}",
                            "code_tag": f"tag-{i}",
                        }[key]
                        for key in columns
                    )
                    for i in range(1, count)
                ],
            )
    return mesh


@pytest.mark.asyncio
async def test_incomplete_recipient_page_preserves_the_last_complete_interest(events):
    replica(events, count=RECIPIENT_PAGE)
    stamp = utc_text(utc_now() - timedelta(minutes=1))
    with sqlite3.connect(events.path) as db:
        db.execute(
            "INSERT INTO mcp_event_mesh_message_interest VALUES('source',"
            "'prior','prior-agent',?,?)",
            (stamp, utc_text(utc_now() + timedelta(minutes=5))),
        )
    recipients = [
        {
            "slot_id": "slot" if i == 0 else f"slot-{i}",
            "recipient_agent_id": "recipient" if i == 0 else f"recipient-{i}",
            "created_at": stamp,
        }
        for i in range(RECIPIENT_PAGE)
    ]

    result = await lifecycle(events).message_events.receive_interest(
        "source",
        {
            "version": 2,
            "offset": 0,
            "complete": False,
            "recipients": recipients,
        },
    )

    assert result == {"ok": True, "next_offset": RECIPIENT_PAGE, "complete": False}
    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT slot_id,recipient_agent_id FROM mcp_event_mesh_message_interest"
        ).fetchall() == [("prior", "prior-agent")]


@pytest.mark.asyncio
async def test_private_interest_backfills_only_current_messages_since_its_start(events):
    replica(events)
    AccessMeshMessageStore(events.path, NODE)
    now = utc_now()
    start = utc_text(now - timedelta(minutes=1))
    with sqlite3.connect(events.path) as db:
        for label, stamp in [("old", utc_text(now - timedelta(minutes=2))), ("current", start)]:
            message_id = NODE + ":localmsg:" + label
            db.execute(
                "INSERT INTO coordination_messages(message_hash,sender_agent_"
                "id,text,created_at,delivery_mode) "
                "VALUES(?,'sender',?,?,'notify')",
                (message_id, label + " text", stamp),
            )
            db.execute(
                "INSERT INTO coordination_message_recipients(message_hash,rec"
                "ipient_agent_id) VALUES(?,'recipient')",
                (message_id,),
            )
            db.execute(
                "INSERT INTO access_mesh_messages(message_hash,wire_json,orig"
                "in_node_id,receipt_json) VALUES(?,?,?,?)",
                (
                    message_id,
                    json.dumps({"sender_name": "Sender"}),
                    NODE,
                    json.dumps(
                        {
                            "deliveries": [
                                {"logical_agent_id": "recipient", "public_name": "Recipient"}
                            ]
                        }
                    ),
                ),
            )

    await lifecycle(events).message_events.receive_interest(
        "source",
        {
            "version": 1,
            "offset": 0,
            "complete": True,
            "recipients": [
                {"slot_id": "slot", "recipient_agent_id": "recipient", "created_at": start}
            ],
        },
    )

    with sqlite3.connect(events.path) as db:
        rows = db.execute("SELECT data_json FROM mcp_event_records").fetchall()
    assert [json.loads(row[0])["text"] for row in rows] == ["current text"]

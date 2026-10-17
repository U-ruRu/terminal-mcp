"""Atomic subscription ownership, inbox consumption and recipient pinning."""

import asyncio
import base64
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.mcp.message_binding import MeshMessageSubscriberResolver, MessageSubscriber
from terminal_mcp.storage.access_mesh import AccessMeshStore
from terminal_mcp.storage.sqlite import SqliteRepository

NODE = "access"
CALLBACK = "https://receiver.example.test/events"
SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode()


@pytest_asyncio.fixture
async def events(tmp_path):
    repo = SqliteRepository(tmp_path / "events.sqlite3")
    await repo.initialize()
    store = MCPEventStore(repo.path, NODE)
    await store.initialize()
    return store


def binding(agent="recipient", *, issuer=NODE):
    return MessageSubscriber(agent, "", 0, "session-number:1234", issuer, "slot", "session_number")


async def subscription(
    store, sub_id="sub", *, agent="recipient", principal="owner", name="message.received"
):
    await store.save_subscription(
        sub_id=sub_id,
        principal=principal,
        credential_id="credential",
        auth_mode="bearer",
        name=name,
        subscriber_agent_id=agent,
        callback=CALLBACK + "/" + sub_id,
        secret=SECRET,
        no_expiry=True,
        message_binding=binding() if name == "message.received" else None,
        authorization_checker=AsyncMock(return_value=True),
        arguments={"session_number": "1234"} if name == "message.received" else {},
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE mcp_event_subscriptions SET created_at=? WHERE id=?",
            (utc_text(utc_now() - timedelta(minutes=2)), sub_id),
        )


def record(
    store,
    event_id="evt_" + "1" * 32,
    *,
    subscriptions=("sub",),
    name="message.received",
    read=False,
):
    stamp = utc_text(utc_now() - timedelta(seconds=30))
    data = {
        "server": NODE,
        "sender": "Sender",
        "recipient": "Recipient",
        "message_id": "access:localmsg:one",
        "text": "Current message",
    }
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO mcp_event_records(event_id,source_key,name,occur"
            "red_at,data_json,recipient_agent_id) "
            "VALUES(?,?,?,?,?,?)",
            (event_id, event_id, name, stamp, json.dumps(data), "recipient"),
        )
        db.executemany(
            "INSERT INTO mcp_event_deliveries(event_id,subscription_id,ne"
            "xt_attempt_at,read_at) VALUES(?,?,?,?)",
            [(event_id, sub_id, stamp, stamp if read else None) for sub_id in subscriptions],
        )
    return event_id


def admission():
    return VerifiedAdmissionContext(
        principal_id="owner",
        credential_id="credential",
        scopes=frozenset({"terminal:read"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="bearer",
    )


def subscribe_request():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "events/subscribe",
        "params": {
            "name": "message.received",
            "arguments": {"session_number": "1234"},
            "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
        },
    }


@pytest.mark.asyncio
async def test_event_page_consumes_one_unique_event_across_callback_fanout(events):
    await subscription(events, "first")
    await subscription(events, "second")
    first_id = record(events, subscriptions=("first", "second"))
    second_id = record(events, "evt_" + "2" * 32, subscriptions=("first",))

    page = await events.get_events("recipient", principal_id="owner", limit=1)

    assert [event["eventId"] for event in page["events"]] == [first_id]
    assert page["has_more"] and page["next_cursor"]
    with sqlite3.connect(events.path) as db:
        reads = db.execute(
            "SELECT event_id,read_at IS NOT NULL FROM mcp_event_deliveries ORDER BY event_id"
        ).fetchall()
    assert reads == [(first_id, 1), (first_id, 1), (second_id, 0)]


@pytest.mark.asyncio
async def test_event_inbox_is_scoped_to_both_subscriber_and_principal(events):
    await subscription(events, "mine")
    await subscription(events, "other-principal", principal="outsider")
    await subscription(events, "other-agent", agent="someone-else")
    expected = record(events, subscriptions=("mine",))
    record(events, "evt_" + "2" * 32, subscriptions=("other-principal",))
    record(events, "evt_" + "3" * 32, subscriptions=("other-agent",))

    page = await events.get_events("recipient", principal_id="owner")

    assert [event["eventId"] for event in page["events"]] == [expected]


@pytest.mark.asyncio
async def test_global_lifecycle_event_is_already_read_for_another_agent_of_the_principal(events):
    await subscription(events, agent=None, name="agent.attached")
    record(events, name="agent.attached", read=True)

    page = await events.get_events("another-agent", principal_id="owner")

    assert page["events"] == []


@pytest.mark.asyncio
async def test_event_id_read_returns_the_previously_read_message(events):
    await subscription(events)
    event_id = record(events, read=True)

    result = await events.get_events("recipient", principal_id="owner", event_id=event_id)

    assert result["events"][0]["read"] is True
    assert result["events"][0]["data"]["text"] == "Current message"


@pytest.mark.asyncio
async def test_unsubscribe_requires_the_subscription_principal(events):
    await subscription(events)

    await events.unsubscribe("sub", "outsider")

    assert (await events.subscription("sub"))["principal_id"] == "owner"


@pytest.mark.asyncio
async def test_subscription_rechecks_recipient_identity_after_the_callback_challenge(
    events, resolver
):
    async def challenge(url, body, headers):
        resolver.mesh.store.code_slot.return_value.logical_agent_id = "replacement-recipient"
        return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()

    adapter = MCPEventsAdapter(
        None,
        events,
        sender=challenge,
        access_checker=resolver.still_bound,
        message_resolver=resolver,
        endpoint_role="access",
    )
    token = bind_admission_context(admission())
    try:
        result = await adapter.handle(subscribe_request())
    finally:
        reset_admission_context(token)

    assert result["error"]["code"] == -32001
    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_refresh_keeps_the_original_recipient_when_a_number_is_reassigned(events):
    await subscription(events)
    with sqlite3.connect(events.path) as db:
        db.execute("UPDATE mcp_event_subscriptions SET callback_url=? WHERE id='sub'", (CALLBACK,))
    resolver = SimpleNamespace(still_bound=AsyncMock(return_value=False))
    adapter = MCPEventsAdapter(None, events, message_resolver=resolver, endpoint_role="access")
    token = bind_admission_context(admission())
    try:
        result = await adapter.handle(subscribe_request())
    finally:
        reset_admission_context(token)

    assert result["error"]["data"]["reason"] == "recipient_binding_changed"
    stored = await events.message_subscription_binding(
        "owner", {"session_number": "1234"}, CALLBACK
    )
    assert stored["recipient_agent_id"] == "recipient"


@pytest.mark.asyncio
async def test_revoked_message_delivery_retains_its_owner_removable_binding(events):
    await subscription(events)
    record(events)
    sender = AsyncMock()
    adapter = MCPEventsAdapter(
        None, events, sender=sender, access_checker=AsyncMock(return_value=False)
    )

    await adapter.deliver_due()

    assert (await events.subscription("sub"))["subscriber_agent_id"] == "recipient"
    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT state FROM mcp_event_deliveries").fetchone() == ("dead",)
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_workers_claim_each_webhook_delivery_once(events):
    await subscription(events)
    expected = record(events)

    first, second = await asyncio.gather(events.due_deliveries(), events.due_deliveries())

    assert [item["event_id"] for item in first + second] == [expected]


@pytest.mark.asyncio
async def test_unsubscribe_invalidates_an_already_claimed_dispatch(events):
    await subscription(events)
    record(events)
    (claimed,) = await events.due_deliveries()
    await events.unsubscribe("sub", "owner")

    assert await events.dispatch_is_current(claimed) is False


@pytest.mark.asyncio
async def test_interrupted_webhook_claim_recovers_its_persisted_attempt_count(events):
    await subscription(events)
    record(events)
    with sqlite3.connect(events.path) as db:
        db.execute("UPDATE mcp_event_deliveries SET state='sending',attempts=2")

    await MCPEventStore(events.path, NODE).initialize()

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT state,attempts FROM mcp_event_deliveries").fetchone() == (
            "pending",
            2,
        )


@pytest.mark.asyncio
async def test_transient_callback_failure_schedules_a_durable_retry(events):
    await subscription(events)
    event_id = record(events)

    await events.finish_attempt(event_id, "sub", status=503)

    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT state,attempts,last_status FROM mcp_event_deliveries"
        ).fetchone() == ("pending", 1, 503)


@pytest.mark.asyncio
async def test_successful_webhook_delivery_preserves_the_message_ack_obligation(events):
    await subscription(events)
    record(events)
    with sqlite3.connect(events.path) as db:
        db.execute(
            "INSERT INTO coordination_messages(message_hash,sender_agent_"
            "id,text,created_at,delivery_mode) "
            "VALUES('access:localmsg:one','sender','Current message',?,'ack')",
            (utc_text(),),
        )
        db.execute(
            "INSERT INTO coordination_message_recipients(message_hash,recipient_agent_id) "
            "VALUES('access:localmsg:one','recipient')"
        )
    adapter = MCPEventsAdapter(
        None,
        events,
        sender=AsyncMock(return_value=(202, b"")),
        access_checker=AsyncMock(return_value=True),
    )

    await adapter.deliver_due()

    with sqlite3.connect(events.path) as db:
        assert db.execute("SELECT state FROM mcp_event_deliveries").fetchone() == ("sent",)
        assert db.execute(
            "SELECT read_at,replied_at FROM coordination_message_recipients"
        ).fetchone() == (None, None)


@pytest.fixture
def resolver():
    slot = SimpleNamespace(
        issuer_id=NODE, slot_id="slot", logical_agent_id="recipient", state="active"
    )
    store = SimpleNamespace(
        local_node_id=NODE,
        code_slot=Mock(return_value=slot),
        numbers=SimpleNamespace(
            winner=Mock(
                return_value={"issuer_id": NODE, "slot_id": "slot", "logical_agent_id": "recipient"}
            )
        ),
        merged_cycle=Mock(return_value={"state": "active"}),
    )
    return MeshMessageSubscriberResolver(SimpleNamespace(store=store, clock=utc_now))


@pytest.mark.asyncio
async def test_authorized_public_number_resolves_to_an_immutable_recipient(resolver):
    resolved = await resolver.resolve_number(admission(), "1234", "access")

    assert resolved == binding()


@pytest.mark.asyncio
async def test_public_number_resolution_requires_read_scope(resolver):
    resolved = await resolver.resolve_number(
        replace(admission(), scopes=frozenset()), "1234", "access"
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_existing_number_subscription_survives_the_work_window_end(resolver):
    resolver.mesh.store.merged_cycle.return_value = {"state": "inactive"}
    delivery = {
        "recipient_agent_id": "recipient",
        "recipient_issuer_id": NODE,
        "recipient_slot_id": "slot",
        "recipient_role": "session_number",
        "recipient_node_id": NODE,
        "recipient_connection_key": "session-number:1234",
    }

    assert await resolver.still_bound(delivery) is True


@pytest_asyncio.fixture
async def pinned(events):
    mesh = AccessMeshStore(
        events.path,
        local_node_id=NODE,
        trusted_issuers=frozenset({NODE, "source"}),
        proof_key=b"unit-test-mesh-proof-key-32bytes",
    )
    start = utc_now() - timedelta(minutes=2)
    issued = AccessSlotEvent(
        issuer_id=NODE,
        slot_id="slot",
        logical_agent_id="recipient",
        event_id="issued",
        revision=1,
        kind="SlotIssued",
        policy=SlotPolicy(duration_seconds=60, cooldown_seconds=0),
        code_tag=mesh.code_tag(NODE, "1234"),
        effective_at=start,
    )
    mesh.apply_event(issued, authenticated_peer_id=NODE, issued_kind="persistent")
    mesh.numbers.register(
        number="1234",
        issuer_id=NODE,
        slot_id="slot",
        logical_agent_id="recipient",
        started_at=start,
        hard_expires_at=start + timedelta(minutes=1),
    )
    return events, mesh


@pytest.mark.asyncio
async def test_subscription_commit_acquires_the_exact_recipient_hold(pinned):
    events, _ = pinned

    await subscription(events)

    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT recipient_agent_id,issuer_id,slot_id,number,state FRO"
            "M access_mesh_recipient_holds"
        ).fetchone() == ("recipient", NODE, "slot", "1234", "held")


@pytest.mark.asyncio
async def test_unsubscribe_releases_only_its_own_recipient_hold(pinned):
    events, _ = pinned
    await subscription(events, "first")
    await subscription(events, "second")

    await events.unsubscribe("first", "owner")

    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT hold_id,state,revision FROM access_mesh_recipient_holds ORDER BY hold_id"
        ).fetchall() == [("first", "released", 2), ("second", "held", 1)]


@pytest.mark.asyncio
async def test_subscription_hold_prevents_reusing_an_expired_number(pinned):
    events, mesh = pinned
    await subscription(events)

    result = mesh.numbers.reserve(number="1234", attempt_id="new-owner", now=utc_now())

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_subscription_hold_fences_slot_deletion(pinned):
    events, mesh = pinned
    await subscription(events)
    deletion = AccessSlotEvent(
        issuer_id=NODE,
        slot_id="slot",
        logical_agent_id="recipient",
        event_id="delete",
        revision=2,
        kind="SlotDeleted",
    )

    with pytest.raises(AccessMeshError, match="access_mesh_subscription_hold"):
        mesh.apply_event(deletion, authenticated_peer_id=NODE)

    assert mesh.slot(NODE, "slot").state == "active"


def hold_row(**changes):
    return {
        "source_node_id": "source",
        "hold_id": "remote-sub",
        "recipient_agent_id": "remote",
        "issuer_id": "source",
        "slot_id": "remote-slot",
        "number": "5678",
        "state": "held",
        "revision": 1,
        **changes,
    }


@pytest.mark.asyncio
async def test_hold_release_tombstone_rejects_an_older_replica(pinned):
    _, mesh = pinned
    mesh.recipient_holds.merge(
        [hold_row(state="released", revision=2)], authenticated_peer_id="source"
    )

    mesh.recipient_holds.merge([hold_row()], authenticated_peer_id="source")

    with sqlite3.connect(mesh.path) as db:
        assert db.execute("SELECT state,revision FROM access_mesh_recipient_holds").fetchone() == (
            "released",
            2,
        )


@pytest.mark.asyncio
async def test_issuer_subscription_hold_exposes_a_message_recipient_without_an_attachment(pinned):
    _, mesh = pinned
    start = utc_now() - timedelta(minutes=2)
    mesh.apply_event(
        AccessSlotEvent(
            issuer_id="source",
            slot_id="remote-slot",
            logical_agent_id="remote",
            event_id="remote-issued",
            revision=1,
            kind="SlotIssued",
            policy=SlotPolicy(duration_seconds=60, cooldown_seconds=0),
            code_tag=mesh.code_tag("source", "5678"),
            effective_at=start,
        ),
        authenticated_peer_id="source",
        issued_kind="persistent",
    )
    mesh.numbers.register(
        number="5678",
        issuer_id="source",
        slot_id="remote-slot",
        logical_agent_id="remote",
        started_at=start,
        hard_expires_at=start + timedelta(minutes=1),
    )
    mesh.recipient_holds.merge([hold_row()], authenticated_peer_id="source")

    recipients = mesh.recipient_holds.local_recipients()

    assert recipients == [
        {
            "recipient_agent_id": "remote",
            "issuer_id": "source",
            "slot_id": "remote-slot",
            "number": "5678",
        }
    ]


@pytest.mark.asyncio
async def test_peer_hold_cannot_confer_message_presence_for_another_issuer(pinned):
    _, mesh = pinned
    mesh.recipient_holds.merge(
        [
            hold_row(
                recipient_agent_id="recipient",
                issuer_id=NODE,
                slot_id="slot",
                number="1234",
            )
        ],
        authenticated_peer_id="source",
    )

    assert mesh.recipient_holds.local_recipients() == []


@pytest.mark.asyncio
async def test_callback_http_denial_does_not_create_a_verification_cache(events):
    adapter = MCPEventsAdapter(
        None,
        events,
        sender=AsyncMock(return_value=(403, b"denied")),
        message_resolver=SimpleNamespace(resolve_number=AsyncMock(return_value=binding())),
        access_checker=AsyncMock(return_value=True),
        endpoint_role="access",
    )
    token = bind_admission_context(admission())
    try:
        result = await adapter.handle(subscribe_request())
    finally:
        reset_admission_context(token)

    assert result["error"]["code"] == -32015
    assert await events.verified_recently("owner", CALLBACK) is False


@pytest.mark.asyncio
async def test_binding_write_failure_rolls_back_the_subscription_and_callback_cache(events):
    with sqlite3.connect(events.path) as db:
        db.execute(
            "CREATE TRIGGER fail_binding BEFORE INSERT ON mcp_event_message_bindings "
            "BEGIN SELECT RAISE(ABORT,'binding failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="binding failed"):
        await events.save_subscription(
            sub_id="sub",
            principal="owner",
            credential_id="credential",
            auth_mode="bearer",
            name="message.received",
            subscriber_agent_id="recipient",
            callback=CALLBACK,
            secret=SECRET,
            message_binding=binding(),
            arguments={"session_number": "1234"},
            callback_verified_fresh=True,
            authorization_checker=AsyncMock(return_value=True),
        )

    assert await events.subscription("sub") is None
    assert await events.verified_recently("owner", CALLBACK) is False


@pytest.mark.asyncio
async def test_private_event_retention_keeps_recent_messages_and_old_lifecycle_events(events):
    await subscription(events)
    await subscription(events, "lifecycle", agent=None, name="agent.attached")
    old_message = record(events, "evt_" + "4" * 32)
    current_message = record(events, "evt_" + "5" * 32)
    attached = record(
        events, "evt_" + "6" * 32, subscriptions=("lifecycle",), name="agent.attached"
    )
    now = utc_now()
    with sqlite3.connect(events.path) as db:
        db.execute(
            "UPDATE mcp_event_records SET occurred_at=? WHERE event_id IN (?,?)",
            (utc_text(now - timedelta(hours=24, seconds=1)), old_message, attached),
        )

    await events.pump(now=now)

    with sqlite3.connect(events.path) as db:
        assert db.execute(
            "SELECT event_id FROM mcp_event_records ORDER BY event_id"
        ).fetchall() == [(current_message,), (attached,)]
        assert db.execute(
            "SELECT event_id FROM mcp_event_deliveries ORDER BY event_id"
        ).fetchall() == [(current_message,), (attached,)]


@pytest.mark.asyncio
async def test_oauth_subscription_commit_serializes_with_credential_revocation(events):
    auth_path = events.path.parent / "auth.sqlite3"
    with sqlite3.connect(auth_path) as db:
        db.execute("CREATE TABLE oauth_clients(client_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO oauth_clients VALUES('current-client')")
    events.auth_database_path = auth_path

    async def authorize(candidate):
        with sqlite3.connect(auth_path, timeout=0) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("DELETE FROM oauth_clients WHERE client_id='current-client'")
        return True

    await events.save_subscription(
        sub_id="sub",
        principal="owner",
        credential_id="oauth:current-client",
        auth_mode="oauth",
        name="message.received",
        subscriber_agent_id="recipient",
        callback=CALLBACK,
        secret=SECRET,
        message_binding=binding(),
        arguments={"session_number": "1234"},
        authorization_checker=authorize,
    )

    assert (await events.subscription("sub"))["credential_id"] == "oauth:current-client"

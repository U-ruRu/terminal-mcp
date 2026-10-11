"""MCP 2.0 Events acceptance: durable source, idle epochs, signed callbacks."""

import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import parse_utc, utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.mcp.event_webhooks import (
    CallbackEndpointError,
    post_signed,
    signed_headers,
    validate_callback_url,
)
from terminal_mcp.mcp.message_binding import MessageSubscriber
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository

NODE = "secondary-test"
CALLBACK = "https://receiver.example.test/mcp-events"
SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode()


@pytest.fixture
async def context(tmp_path):
    path = tmp_path / "events.db"
    repo = SqliteRepository(path)
    await repo.initialize()
    events = MCPEventStore(path, NODE)
    await events.initialize()
    return path, AgentStore(path), events


async def _agent(store, *, agent_id="Alpha-01234567", seconds_ago=1, source="remote"):
    stamp = utc_text(utc_now() - timedelta(seconds=seconds_ago))
    await store.create_session(
        agent_id,
        "private task - MUST NOT LEAK",
        "secret intent",
        [],
        ["sensitive step"],
        1,
        stamp,
        source_instance_id=source,
        global_expires_at=utc_text(utc_now() + timedelta(seconds=1000)),
    )
    return agent_id, stamp


def _records(path, kind=None):
    with sqlite3.connect(path) as db:
        data = db.execute("SELECT event_id,name,data_json FROM mcp_event_records").fetchall()
    rows = [{"event_id": row[0], "name": row[1], "data": json.loads(row[2])} for row in data]
    return [row for row in rows if row["name"] == kind] if kind else rows


@pytest.mark.asyncio
async def test_attach_only_after_local_row_commit_and_no_duplicate(context):
    path, agents, events = context
    assert _records(path) == []
    await _agent(agents)
    assert (await agents.get_session("Alpha-01234567"))["state"] == "active"
    await events.pump()
    attached = _records(path, "agent.attached")
    assert len(attached) == 1
    assert attached[0]["data"] == {"agent_name": "Alpha", "server": NODE}
    await events.pump()
    assert _records(path, "agent.attached") == attached


@pytest.mark.asyncio
async def test_foreign_reactivation_creates_one_more_attach(context):
    path, agents, events = context
    await _agent(agents)
    await events.pump()
    await agents.expire("Alpha-01234567", now=utc_text())
    await events.pump()
    changed = await agents.reactivate_foreign_session(
        "Alpha-01234567",
        "task",
        "intent",
        [],
        ["step"],
        1,
        utc_text(),
        registered_at=utc_text(),
        source_instance_id="remote",
        global_expires_at=utc_text(utc_now() + timedelta(seconds=1000)),
        last_activity_at=utc_text(),
        record_task_event=False,
    )
    assert changed
    await events.pump()
    assert len(_records(path, "agent.attached")) == 2
    await events.pump()
    assert len(_records(path, "agent.attached")) == 2


@pytest.mark.asyncio
async def test_strict_idle_single_shot_and_rearm_after_real_activity(context):
    path, agents, events = context
    agent_id, activity = await _agent(agents, seconds_ago=1)
    await events.pump(now=utc_now())
    # Exact comparison against the stored (millisecond-precision) clock.
    limit = parse_utc(activity) + timedelta(seconds=180)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
            (activity, agent_id),
        )
    await events.pump(now=limit)
    assert not _records(path, "agent.inactive")
    await events.pump(now=limit + timedelta(microseconds=1))
    await events.pump(now=limit + timedelta(seconds=50))
    assert len(_records(path, "agent.inactive")) == 1
    new_activity = utc_text(limit + timedelta(seconds=51))
    await agents.touch(agent_id, new_activity)
    await events.pump(now=limit + timedelta(seconds=230))
    assert len(_records(path, "agent.inactive")) == 1
    await events.pump(now=limit + timedelta(seconds=232))
    assert len(_records(path, "agent.inactive")) == 2
    assert activity is not None


@pytest.mark.asyncio
async def test_observer_not_touch_target_or_rearm_idle(context):
    path, agents, events = context
    agent_id, _ = await _agent(agents, seconds_ago=190)
    before = (await agents.get_session(agent_id))["last_activity_at"]
    # The unprivileged observation path only reads the session row.
    await agents.get_session(agent_id)
    assert (await agents.get_session(agent_id))["last_activity_at"] == before
    await events.pump()
    await events.pump()
    assert len(_records(path, "agent.inactive")) == 1


@pytest.mark.asyncio
async def test_legacy_agent_idle_expire_and_finish_are_not_limited_work_session_end(context):
    path, agents, events = context
    await _agent(agents)
    await _agent(agents, agent_id="Bravo-01234567")
    await events.pump()
    assert await agents.finish("Alpha-01234567")
    assert await agents.expire("Bravo-01234567", reason="idle_timeout")
    await events.pump()
    assert not _records(path, "session.ended")


@pytest.mark.asyncio
async def test_remote_grant_is_not_local_attach(context):
    path, _, events = context
    # A grant is a different durable boundary from presence materialization.
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO logical_agent_node_attachments"
            "(node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
            "node_instance_id,authority_epoch,attached_at,hard_expires_at) "
            "VALUES('attach1','logical1','session1',1,'elsewhere',1,?,?)",
            (utc_text(), utc_text(utc_now() + timedelta(hours=1))),
        )
    await events.pump()
    assert not _records(path, "agent.attached")


@pytest.mark.asyncio
async def test_persistent_presence_for_other_node_not_misattributed(context):
    path, _, events = context
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO instance_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "VALUES('mcp.presence.attached','attachment_presence','missing',?,?)",
            (json.dumps({"node": "other-server", "logical_agent_id": "X"}), utc_text()),
        )
    await events.pump()
    assert not _records(path, "agent.attached")


async def access_allowed(_item):
    return True


class FakeReceiver:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
        self.requests = []

    async def __call__(self, url, body, headers):
        data = json.loads(body)
        self.requests.append((url, data, headers))
        if data.get("type") == "verification":
            return 200, json.dumps({"challenge": data["challenge"]}).encode()
        return (self.statuses.pop(0) if self.statuses else 202), b""


def _admission():
    return VerifiedAdmissionContext(
        principal_id="test-user",
        credential_id="bearer:test",
        scopes=frozenset({"terminal:read"}),
        auth_generation=1,
        transport="mcp",
        auth_mode="bearer",
    )


@pytest.mark.asyncio
async def test_subscription_challenge_refresh_unsubscribe_and_persistence(context):
    path, agents, events = context
    receiver = FakeReceiver()
    adapter = MCPEventsAdapter(
        None,
        events,
        sender=receiver,
        access_checker=access_allowed,
    )
    token = bind_admission_context(_admission())
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events/subscribe",
            "params": {
                "name": "agent.attached",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
            },
        }
        response = await adapter.handle(request)
        assert "error" not in response
        sub_id = response["result"]["id"]
        assert len(receiver.requests) == 1
        assert receiver.requests[0][2]["X-MCP-Subscription-Id"] == sub_id
        assert (await adapter.handle(request))["result"]["id"] == sub_id
        assert len(receiver.requests) == 1
        # Cached refresh must preserve the original verification deadline.
        with sqlite3.connect(path) as db:
            original_verified_until = db.execute(
                "SELECT verified_until FROM mcp_event_verified_callbacks "
                "WHERE principal_id=? AND callback_url=?",
                ("test-user", CALLBACK),
            ).fetchone()[0]
        await adapter.handle(request)
        with sqlite3.connect(path) as db:
            cache_until = db.execute(
                "SELECT verified_until FROM mcp_event_verified_callbacks "
                "WHERE principal_id=? AND callback_url=?",
                ("test-user", CALLBACK),
            ).fetchone()[0]
            # Simulate natural expiry without sleeping or touching a live endpoint.
            db.execute(
                "UPDATE mcp_event_verified_callbacks SET verified_until=? "
                "WHERE principal_id=? AND callback_url=?",
                ("2000-01-01T00:00:00Z", "test-user", CALLBACK),
            )
        assert original_verified_until == cache_until
        await adapter.handle(request)
        assert len(receiver.requests) == 2  # A fresh verification took place.
        assert (await events.subscription(sub_id))["principal_id"] == "test-user"
        await _agent(agents)
        await events.pump()
        await adapter.deliver_due()
        assert len(receiver.requests) == 3
        delivery = receiver.requests[2][1]
        assert delivery["eventId"].startswith("evt_")
        assert delivery["data"] == {"agent_name": "Alpha", "server": NODE}
        request["method"] = "events/unsubscribe"
        del request["params"]["delivery"]["secret"]
        assert (await adapter.handle(request))["result"] == {}
        assert await events.subscription(sub_id) is None
    finally:
        reset_admission_context(token)


@pytest.mark.asyncio
async def test_webhook_signature_and_stable_event_id(context):
    path, agents, events = context
    receiver = FakeReceiver()
    adapter = MCPEventsAdapter(
        None,
        events,
        sender=receiver,
        access_checker=access_allowed,
    )
    token = bind_admission_context(_admission())
    try:
        request = {
            "id": 5,
            "method": "events/subscribe",
            "jsonrpc": "2.0",
            "params": {
                "name": "agent.attached",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
            },
        }
        response = await adapter.handle(request)
        assert "result" in response
        await _agent(agents)
        await events.pump()
        await adapter.deliver_due()
        _, payload, headers = receiver.requests[-1]
        assert headers["webhook-id"] == payload["eventId"]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        signature = hmac.new(
            b"x" * 32,
            headers["webhook-id"].encode()
            + b"."
            + headers["webhook-timestamp"].encode()
            + b"."
            + body,
            hashlib.sha256,
        ).digest()
        assert headers["webhook-signature"] == ("v1," + base64.b64encode(signature).decode())
    finally:
        reset_admission_context(token)


@pytest.mark.asyncio
async def test_delivery_backoff_and_no_retry_410_413(context):
    path, agents, events = context
    await _agent(agents)
    await events.pump()
    for status in (410, 413, 500):
        receiver = FakeReceiver([status])
        adapter = MCPEventsAdapter(
            None,
            events,
            sender=receiver,
            access_checker=access_allowed,
        )
        token = bind_admission_context(_admission())
        try:
            sub = await adapter.handle(
                {
                    "id": 1,
                    "jsonrpc": "2.0",
                    "method": "events/subscribe",
                    "params": {
                        "name": "agent.inactive",
                        "arguments": {},
                        "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
                    },
                }
            )
            assert "result" in sub
            # Manually queue a delivery for an event after the subscription.
            with sqlite3.connect(path) as db:
                db.execute("UPDATE mcp_event_records SET name='agent.inactive'")
                db.execute("UPDATE mcp_event_records SET occurred_at=?", (utc_text(),))
                db.execute(
                    "INSERT OR REPLACE INTO mcp_event_deliveries"
                    "(event_id,subscription_id,next_attempt_at) "
                    "SELECT event_id,?,? FROM mcp_event_records",
                    (sub["result"]["id"], utc_text()),
                )
            await adapter.deliver_due()
            with sqlite3.connect(path) as db:
                state, attempts = db.execute(
                    "SELECT state,attempts FROM mcp_event_deliveries WHERE subscription_id=?",
                    (sub["result"]["id"],),
                ).fetchone()
            assert (state, attempts) == (("pending" if status == 500 else "dead"), 1)
            await events.unsubscribe(sub["result"]["id"], "test-user")
        finally:
            reset_admission_context(token)


def test_https_and_private_address_rejected():
    for url in (
        "http://receiver.example.test",
        "https://localhost/#x",
        "https://u:p@example.org",
        "file:///tmp/secret",
    ):
        with pytest.raises(CallbackEndpointError):
            validate_callback_url(url)


@pytest.mark.asyncio
async def test_no_auth_subscription_rejected(context):
    _, _, store = context
    adapter = MCPEventsAdapter(None, store)
    response = await adapter.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events/subscribe",
            "params": {
                "name": "agent.inactive",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
            },
        }
    )
    assert response["error"]["code"] == -32001


def test_standard_webhooks_signature_uses_exact_body():
    body = b'{"some":"data"}'
    headers = signed_headers(SECRET, "evt_test", body, "sub_test")
    expected = hmac.new(
        b"x" * 32,
        f"evt_test.{headers['webhook-timestamp']}.".encode() + body,
        hashlib.sha256,
    ).digest()
    assert headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode()


def _seed_persistent(path, *, nodes=("secondary-test",), state="active"):
    """Use real schema/trigger boundaries without privileged or Mesh credentials."""
    now = utc_text()
    expires = utc_text(utc_now() + timedelta(minutes=23))
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO logical_agents(logical_agent_id,display_name,state,"
            "authority_node_id,authority_epoch,created_at,updated_at) "
            "VALUES('la1','Persistent Agent','active',?,1,?,?)",
            (NODE, now, now),
        )
        db.execute(
            "INSERT INTO logical_agent_work_sessions(work_session_id,logical_agent_id,"
            "session_epoch,authority_node_id,authority_epoch,started_at,"
            "hard_expires_at,auth_generation,state) "
            "VALUES('ws1','la1',1,?,1,?,?,1,?)",
            (NODE, now, expires, state),
        )
        for i, node in enumerate(nodes):
            identifier = f"p_att_{i}"
            db.execute(
                "INSERT INTO logical_agent_node_attachments("
                "node_attachment_id,logical_agent_id,work_session_id,"
                "session_epoch,node_instance_id,authority_epoch,attached_at,hard_expires_at)"
                "VALUES(?,'la1','ws1',1,?,1,?,?)",
                (identifier, node, now, expires),
            )
            db.execute(
                "INSERT INTO persistent_attachment_presence("
                "node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
                "node_instance_id,task_summary,intent,work_scope_json,details_json,"
                "current_step,intent_updated_at,last_activity_at)"
                "VALUES(?,'la1','ws1',1,?,'secret task','private intent','[]','[]',1,?,?)",
                (identifier, node, now, now),
            )
    return now


@pytest.mark.asyncio
async def test_real_persistent_presence_and_session_expiry_per_local_node(context):
    path, _, events = context
    _seed_persistent(path, nodes=(NODE, "some-other-server"))
    with sqlite3.connect(path) as db:
        local = db.execute(
            "SELECT node_instance_id FROM persistent_attachment_presence "
            "WHERE node_attachment_id='p_att_0'"
        ).fetchone()
    assert local == (NODE,)
    await events.pump()
    assert [r["data"] for r in _records(path, "agent.attached")] == [
        {"agent_name": "Persistent Agent", "server": NODE}
    ]
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions "
            "SET state='expired',end_reason='hard_duration',ended_at=? "
            "WHERE work_session_id='ws1'",
            (utc_text(),),
        )
    await events.pump()
    assert [r["data"] for r in _records(path, "session.ended")] == [
        {"agent_name": "Persistent Agent", "server": NODE}
    ]
    await events.pump()
    assert len(_records(path, "session.ended")) == 1


@pytest.mark.asyncio
async def test_real_limited_work_session_explicit_end_delivers_once(context):
    path, _, events = context
    _seed_persistent(path)
    await events.pump()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET state='ended',"
            "end_reason='session_end',ended_at=? WHERE work_session_id='ws1'",
            (utc_text(),),
        )
    await events.pump()
    assert [r["data"] for r in _records(path, "session.ended")] == [
        {"agent_name": "Persistent Agent", "server": NODE}
    ]
    await events.pump()
    assert len(_records(path, "session.ended")) == 1
    # A post-finalization metadata update is a new journal row,
    # but not a second WorkSession lifecycle transition.
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE logical_agent_work_sessions SET end_reason='cleanup' "
            "WHERE work_session_id='ws1'"
        )
    await events.pump()
    assert len(_records(path, "session.ended")) == 1


@pytest.mark.asyncio
async def test_persistent_attachment_stays_untouched_by_read_and_idles(context):
    path, _, events = context
    now = _seed_persistent(path)
    earlier = utc_text(utc_now() - timedelta(seconds=181))
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE persistent_attachment_presence SET last_activity_at=?",
            (earlier,),
        )
        before = db.execute(
            "SELECT last_activity_at FROM persistent_attachment_presence"
        ).fetchone()
        db.execute("SELECT * FROM persistent_attachment_presence").fetchall()
        after = db.execute("SELECT last_activity_at FROM persistent_attachment_presence").fetchone()
        assert before == after == (earlier,)
    await events.pump()
    assert len(_records(path, "agent.inactive")) == 1
    await events.pump()
    assert len(_records(path, "agent.inactive")) == 1
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE persistent_attachment_presence SET last_activity_at=?",
            (now,),
        )
    await events.pump(now=utc_now() + timedelta(seconds=181))
    assert len(_records(path, "agent.inactive")) == 2


@pytest.mark.asyncio
async def test_persistent_remote_presence_grant_not_attached_to_local(context):
    path, _, events = context
    _seed_persistent(path, nodes=("elsewhere",))
    with sqlite3.connect(path) as db:
        # A foreign-authority WorkSession and remote-only presence are not a
        # locally materialized attachment even if their grants are visible.
        db.execute(
            "UPDATE logical_agent_work_sessions SET authority_node_id='elsewhere',"
            "origin_instance_id='elsewhere' WHERE work_session_id='ws1'"
        )
    await events.pump()
    assert _records(path, "agent.attached") == []


@pytest.mark.asyncio
async def test_challenge_failure_rejects_subscription_and_does_not_store(context):
    _, _, events = context

    async def bad_receiver(url, body, headers):
        return 200, b'{"challenge":"different"}'

    adapter = MCPEventsAdapter(None, events, sender=bad_receiver)
    token = bind_admission_context(_admission())
    try:
        response = await adapter.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "events/subscribe",
                "params": {
                    "name": "agent.inactive",
                    "arguments": {},
                    "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
                },
            }
        )
        assert response["error"]["code"] == -32015
        assert response["error"]["data"]["reason"] == "challenge_failed"
        assert (
            await events.subscription(adapter._id("test-user", "agent.inactive", {}, CALLBACK))
            is None
        )
    finally:
        reset_admission_context(token)


@pytest.mark.asyncio
async def test_missing_callback_credentials_rejected_without_network(context):
    _, _, events = context
    receiver = FakeReceiver()
    adapter = MCPEventsAdapter(
        None,
        events,
        sender=receiver,
        access_checker=access_allowed,
    )
    token = bind_admission_context(_admission())
    try:
        response = await adapter.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "events/subscribe",
                "params": {
                    "name": "agent.attached",
                    "arguments": {},
                    "delivery": {"mode": "webhook", "url": CALLBACK, "secret": "whsec_bad"},
                },
            }
        )
        assert response["error"]["code"] == -32602
        assert receiver.requests == []
    finally:
        reset_admission_context(token)


@pytest.mark.asyncio
async def test_private_dns_answer_blocks_outbound_connect(monkeypatch):
    import socket

    def private_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_dns)
    with pytest.raises(CallbackEndpointError, match="unsafe_address"):
        await post_signed(
            CALLBACK,
            SECRET,
            "sub_test",
            "evt_test",
            {
                "eventId": "evt_test",
                "name": "agent.inactive",
                "data": {"agent_name": "A", "server": NODE},
                "timestamp": utc_text(),
                "cursor": None,
            },
        )


@pytest.mark.asyncio
async def test_revoked_subscription_denied_before_network_delivery(context):
    path, agents, store = context
    delivered = FakeReceiver()
    access = True
    checks = []

    async def guard(item):
        checks.append((item["principal_id"], item["credential_id"]))
        return access

    adapter = MCPEventsAdapter(None, store, sender=delivered, access_checker=guard)
    token = bind_admission_context(_admission())
    try:
        res = await adapter.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "events/subscribe",
                "params": {
                    "name": "agent.attached",
                    "arguments": {},
                    "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
                },
            }
        )
        sub = res["result"]["id"]
    finally:
        reset_admission_context(token)
    assert len(delivered.requests) == 1  # verification request only
    await _agent(agents)
    await store.pump()
    access = False
    await adapter.deliver_due()
    assert len(delivered.requests) == 1  # no event exfiltrated after revoke
    # Both durable subscription creation and delivery revalidate access.
    assert checks == [("test-user", "bearer:test")] * 2
    assert await store.subscription(sub) is None


@pytest.mark.asyncio
async def test_fail_closed_when_delivery_auth_check_unavailable(context):
    path, agents, store = context
    receiver = FakeReceiver()
    adapter = MCPEventsAdapter(None, store, sender=receiver)
    token = bind_admission_context(_admission())
    try:
        sub = await adapter.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "events/subscribe",
                "params": {
                    "name": "agent.attached",
                    "arguments": {},
                    "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
                },
            }
        )
        assert "result" in sub
    finally:
        reset_admission_context(token)
    await _agent(agents)
    await store.pump()
    await adapter.deliver_due()
    assert len(receiver.requests) == 1
    assert await store.subscription(sub["result"]["id"]) is None


@pytest.mark.asyncio
async def test_callback_secret_rotation_dual_signatures(context):
    path, agents, store = context
    receiver = FakeReceiver()
    adapter = MCPEventsAdapter(None, store, sender=receiver, access_checker=access_allowed)
    new_key = "whsec_" + base64.b64encode(b"y" * 32).decode()
    token = bind_admission_context(_admission())
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "events/subscribe",
            "params": {
                "name": "agent.attached",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET},
            },
        }
        first = await adapter.handle(payload)
        assert "result" in first
        payload["params"]["delivery"]["secret"] = new_key
        second = await adapter.handle(payload)
        assert second["result"]["id"] == first["result"]["id"]
    finally:
        reset_admission_context(token)
    await _agent(agents)
    await store.pump()
    await adapter.deliver_due()
    _, data, headers = receiver.requests[-1]
    parts = headers["webhook-signature"].split(" ")
    assert len(parts) == 2
    assert parts[0].startswith("v1,") and parts[1].startswith("v1,")
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    for i, secret in enumerate((b"y" * 32, b"x" * 32)):
        actual = hmac.new(
            secret,
            (headers["webhook-id"] + "." + headers["webhook-timestamp"] + ".").encode() + body,
            hashlib.sha256,
        ).digest()
        assert parts[i] == "v1," + base64.b64encode(actual).decode()


@pytest.mark.asyncio
async def test_subscription_auth_schema_upgrade_fails_closed(context):
    path, _, store = context
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO mcp_event_subscriptions "
            "(id,principal_id,name,arguments_json,callback_url,"
            "signing_secret,created_at,expires_at) "
            "VALUES('old','legacy','agent.attached','{}',?,?,?,?)",
            (CALLBACK, SECRET, utc_text(), utc_text(utc_now() + timedelta(hours=1))),
        )
    await store.initialize()
    migrated = await store.subscription("old")
    assert migrated["credential_id"] is None
    assert migrated["auth_mode"] is None
    assert migrated["previous_signing_secret"] is None


@pytest.mark.asyncio
async def test_message_unsubscribe_after_binding_loss_uses_owner_not_active_mesh(context):
    path, _, events = context
    adapter = MCPEventsAdapter(None, events, message_resolver=None)
    # Create durable rows representing already-established issuer bindings.
    # They are intentionally unresolvable at unsubscribe time.
    for principal, recipient in (("test-user", "agent-a"), ("other-user", "agent-b")):
        binding = MessageSubscriber(
            recipient_agent_id=recipient,
            work_session_id="",
            session_epoch=0,
            connection_key="former-" + recipient,
            issuer_id="secondary-test",
            slot_id="slot-" + recipient,
            role="access",
        )
        sid = adapter._id(principal, "message.received", {}, CALLBACK, binding=binding)
        await events.save_subscription(
            sub_id=sid,
            principal=principal,
            credential_id="test-credential",
            auth_mode="test",
            name="message.received",
            callback=CALLBACK,
            secret=SECRET,
            message_binding=binding,
            authorization_checker=access_allowed,
        )
    token = bind_admission_context(_admission())
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "events/unsubscribe",
            "params": {
                "name": "message.received",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": CALLBACK},
            },
        }
        assert (await adapter.handle(request))["result"] == {}
        assert (await adapter.handle(request))["result"] == {}  # idempotent
    finally:
        reset_admission_context(token)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT principal_id FROM mcp_event_subscriptions").fetchall() == [
            ("other-user",)
        ]
        assert db.execute("SELECT COUNT(*) FROM mcp_event_message_bindings").fetchone()[0] == 1

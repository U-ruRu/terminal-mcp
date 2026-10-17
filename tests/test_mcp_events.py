"""MCP 2.0 Events acceptance: durable source, idle epochs, signed callbacks."""

import base64
import json
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.mcp.event_server import MCPEventsAdapter
from terminal_mcp.mcp.event_store import MCPEventStore, public_event_data
from terminal_mcp.mcp.event_webhooks import (
    CallbackEndpointError,
    post_signed,
)
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository

NODE = "secondary-test"
CALLBACK = "https://receiver.example.test/mcp-events"
SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode()


@pytest.mark.parametrize(
    ("name", "extra", "expected"),
    [
        (
            "agent.attached",
            {"logical_agent_id": "secret", "session_epoch": 9},
            {"server": NODE, "agent_name": "Alpha"},
        ),
        (
            "agent.inactive",
            {"observation": "absent", "message_id": "wrong"},
            {"server": NODE, "agent_name": "Alpha"},
        ),
        (
            "session.ended",
            {"reason": "manual", "observation_time": "unknown"},
            {"server": NODE, "agent_name": "Alpha"},
        ),
        (
            "session.ended",
            {"reason": "expired"},
            {"server": NODE, "agent_name": "Alpha", "reason": "expired"},
        ),
        (
            "message.received",
            {
                "timestamp": "duplicate",
                "logical_agent_id": "secret",
                "session_epoch": 9,
                "text": "unknown",
            },
            {
                "server": NODE,
                "sender": "Bravo",
                "recipient": "Alpha",
                "message_id": "msg-1",
                "text": "unknown",
            },
        ),
    ],
)
def test_compact_public_event_data_keeps_content_without_private_metadata(name, extra, expected):
    record = {
        "server": NODE,
        "agent_name": "Alpha",
        "sender": "Bravo",
        "recipient": "Alpha",
        "message_id": "msg-1",
        "text": "full text",
        **extra,
    }
    assert public_event_data(name, record) == expected


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

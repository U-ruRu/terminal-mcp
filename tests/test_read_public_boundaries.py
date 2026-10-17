"""Current local message reads commit only a successfully encoded response page."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
IDENTITY = {"logical_agent_id": "receiver", "public_name": "Receiver"}


@pytest_asyncio.fixture
async def inbox(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    await SqliteRepository(path).initialize()
    mesh = SimpleNamespace(store=SimpleNamespace(local_node_id="node"), clock=lambda: NOW)
    messages = AccessMeshMessaging(mesh, AgentStore(path))
    wire = {
        "origin_node_id": "node",
        "created_at": NOW.isoformat(),
        "sender_id": "sender",
        "sender_name": "Sender",
        "text": "Current message",
        "target": "Receiver",
        "mode": "notify",
        "scope": "local",
        "require_reply": False,
        "alert": False,
        "namespace": None,
        "task_id": None,
        "reply_to": None,
    }
    messages.store.accept({**wire, "message_hash": "node:localmsg:first"}, [IDENTITY])
    messages.store.accept({**wire, "message_hash": "node:localmsg:second"}, [IDENTITY])
    return messages


async def read(messages, preflight):
    return await messages.read(
        IDENTITY,
        history=False,
        limit=1,
        cursor=None,
        detail="full",
        namespace=None,
        task_id=None,
        response_preflight=preflight,
    )


@pytest.mark.asyncio
async def test_message_final_envelope_failure_preserves_the_unread_inbox(inbox):
    failure = {"ok": False, "code": "output_item_too_large"}

    result = await read(inbox, lambda _page: failure)

    assert result == failure
    with inbox.store.connect() as db:
        rows = db.execute(
            "SELECT message_hash,seen_count,read_at FROM coordination_mes"
            "sage_recipients ORDER BY message_hash"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("node:localmsg:first", 0, None),
        ("node:localmsg:second", 0, None),
    ]


@pytest.mark.asyncio
async def test_message_read_surfaces_only_the_returned_page_after_preflight(inbox):
    preflight_pages = []

    def accept(page):
        preflight_pages.append([row["message_hash"] for row in page["messages"]])
        return None

    result = await read(inbox, accept)

    assert result["ok"] is True
    assert preflight_pages == [["node:localmsg:second"]]
    with inbox.store.connect() as db:
        rows = db.execute(
            "SELECT message_hash,seen_count,read_at IS NOT NULL FROM coor"
            "dination_message_recipients ORDER BY message_hash"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("node:localmsg:first", 0, 0),
        ("node:localmsg:second", 1, 1),
    ]

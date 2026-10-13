"""Current local message transactions; one database, no application or peer runtime."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore, MeshMessagingError
from terminal_mcp.storage.sqlite import SqliteRepository

NOW = datetime(2026, 10, 8, 10, tzinfo=UTC)
WIRE = {
    "message_hash": "node-a:localmsg:" + "a" * 32,
    "origin_node_id": "node-a",
    "created_at": NOW.isoformat(),
    "sender_id": "sender",
    "sender_name": "Sender",
    "text": "local message",
    "target": "broadcast",
    "mode": "notify",
    "scope": "local",
    "require_reply": False,
    "alert": False,
    "namespace": None,
    "task_id": None,
    "reply_to": None,
}
RECIPIENT = {"logical_agent_id": "receiver", "public_name": "Receiver"}


@pytest_asyncio.fixture
async def store(tmp_path):
    path = tmp_path / "messages.sqlite3"
    await SqliteRepository(path, tmp_path / "output.sqlite3").initialize()
    return AccessMeshMessageStore(path, "node-a", now_provider=lambda: NOW)


def row_count(store, table):
    with store.connect() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_reopened_store_preserves_committed_local_message_and_receipt(store):
    receipt = store.accept(WIRE, [RECIPIENT])
    reopened = AccessMeshMessageStore(store.path, "node-a", now_provider=lambda: NOW)
    assert reopened.wire(WIRE["message_hash"]) == WIRE
    assert reopened.receipt(WIRE["message_hash"]) == receipt


def test_same_wire_retry_has_one_message_and_recipient_effect(store):
    first = store.accept(WIRE, [RECIPIENT])
    assert store.accept(WIRE, [RECIPIENT]) == first
    assert row_count(store, "coordination_messages") == 1
    assert row_count(store, "coordination_message_recipients") == 1


def test_changed_wire_retry_cannot_overwrite_committed_message(store):
    store.accept(WIRE, [RECIPIENT])
    with pytest.raises(MeshMessagingError, match="idempotency_conflict"):
        store.accept({**WIRE, "text": "different message"}, [RECIPIENT])
    assert store.wire(WIRE["message_hash"]) == WIRE
    assert row_count(store, "coordination_messages") == 1


def test_oversized_acceptance_receipt_rolls_back_message_and_recipients(store):
    recipients = [
        {
            "logical_agent_id": f"{index}:" + "a" * 150,
            "public_name": "\u754c" * 40 + f"-{index:012d}",
        }
        for index in range(256)
    ]
    with pytest.raises(MeshMessagingError, match="output_item_too_large"):
        store.accept(WIRE, recipients)
    assert store.wire(WIRE["message_hash"]) is None
    assert row_count(store, "coordination_messages") == 0
    assert row_count(store, "coordination_message_recipients") == 0

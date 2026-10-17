"""Current local message transactions; one database, no application or peer runtime."""

from datetime import UTC, datetime, timedelta

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


def test_notify_surface_marks_message_read(store):
    store.accept(WIRE, [RECIPIENT])
    stamp = (NOW + timedelta(seconds=1)).isoformat()

    store.surface("receiver", [WIRE["message_hash"]], stamp)

    row = store.recipient(WIRE["message_hash"], "receiver")
    assert row["first_seen_at"] == row["read_at"] == stamp
    assert row["seen_count"] == 1


def test_ack_preserves_first_receipt_on_retry(store):
    store.accept({**WIRE, "mode": "ack"}, [RECIPIENT])
    store.acknowledge(WIRE["message_hash"], "receiver", NOW.isoformat())
    before = store.recipient(WIRE["message_hash"], "receiver")

    store.acknowledge(WIRE["message_hash"], "receiver", (NOW + timedelta(hours=6)).isoformat())

    assert store.recipient(WIRE["message_hash"], "receiver") == before


def test_alert_ack_keeps_reply_obligation(store):
    store.accept({**WIRE, "mode": "alert", "alert": True, "require_reply": True}, [RECIPIENT])

    store.acknowledge(WIRE["message_hash"], "receiver", NOW.isoformat())

    assert [row["message_hash"] for row in store.page("receiver")] == [WIRE["message_hash"]]


def reply_wire():
    return {
        **WIRE,
        "message_hash": "node-a:localmsg:" + "b" * 32,
        "sender_id": "receiver",
        "sender_name": "Receiver",
        "target": "Sender",
        "text": "reply",
        "reply_to": WIRE["message_hash"],
    }


def test_reply_acceptance_completes_parent_receipt(store):
    store.accept({**WIRE, "mode": "alert", "alert": True, "require_reply": True}, [RECIPIENT])
    reply = reply_wire()

    store.accept(
        reply, [{"logical_agent_id": "sender", "public_name": "Sender"}], reply_author="receiver"
    )

    row = store.recipient(WIRE["message_hash"], "receiver")
    assert row["replied_at"] == reply["created_at"]
    assert row["reply_message_hash"] == reply["message_hash"]


def test_reply_failure_preserves_parent_obligation(store, monkeypatch):
    store.accept({**WIRE, "mode": "alert", "alert": True, "require_reply": True}, [RECIPIENT])
    before = store.recipient(WIRE["message_hash"], "receiver")

    def reject_receipt(*args):
        raise RuntimeError("injected receipt failure")

    monkeypatch.setattr(store, "_validate_receipt_recipient", reject_receipt)
    with pytest.raises(RuntimeError, match="injected receipt failure"):
        store.accept(
            reply_wire(),
            [{"logical_agent_id": "sender", "public_name": "Sender"}],
            reply_author="receiver",
        )

    assert store.recipient(WIRE["message_hash"], "receiver") == before
    assert row_count(store, "coordination_messages") == 1


def test_message_expires_at_original_twenty_four_hour_deadline(store):
    store.accept(WIRE, [RECIPIENT])
    store.clock = lambda: NOW + timedelta(hours=24)

    assert store.page("receiver", history=True) == []


def test_prune_removes_expired_local_message(store):
    store.accept(WIRE, [RECIPIENT])

    removed = store.prune_expired(now=NOW + timedelta(hours=24))

    assert removed == 1
    assert row_count(store, "coordination_messages") == 0

"""Current persistent-store schema and durable request-key state transitions."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    await store.create_slot("logical-1", "Alpha", "A123", authority_node_id="node-a")
    return repo.path, store


def test_current_persistent_schema_contains_durable_obligation_tables(store):
    path, _store = store
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "persistent_idempotency",
        "persistent_agent_audit",
        "logical_agent_node_attachments",
        "persistent_command_permits",
    } <= tables


async def test_same_key_and_payload_replays_the_committed_result(store):
    _path, store = store
    fingerprint = store.idempotency_fingerprint({"expected_revision": 1})
    result = {"ok": True, "slot_revision": 2}
    assert (
        await store.idempotency_put("logical-1", "play", "idem-0001", fingerprint, result) == result
    )
    assert await store.idempotency_get("logical-1", "play", "idem-0001", fingerprint) == result
    assert (
        await store.idempotency_put(
            "logical-1", "play", "idem-0001", fingerprint, {"ok": True, "slot_revision": 999}
        )
        == result
    )


async def test_same_key_with_changed_payload_is_a_conflict(store):
    _path, store = store
    fingerprint = store.idempotency_fingerprint({"expected_revision": 1})
    await store.idempotency_put(
        "logical-1", "play", "idem-0001", fingerprint, {"ok": True, "slot_revision": 2}
    )
    with pytest.raises(PersistentStoreError, match="idempotency_conflict"):
        await store.idempotency_get(
            "logical-1",
            "play",
            "idem-0001",
            store.idempotency_fingerprint({"expected_revision": 2}),
        )


async def test_pending_reservation_blocks_replay_and_can_abort_then_complete(store):
    _path, store = store
    fingerprint = store.idempotency_fingerprint({"expected_revision": 1})
    key = "idem-pending-0001"
    assert await store.idempotency_reserve("logical-1", "suspend", key, fingerprint) is None
    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_get("logical-1", "suspend", key, fingerprint)
    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_reserve("logical-1", "suspend", key, fingerprint)
    assert await store.idempotency_abort("logical-1", "suspend", key, fingerprint)
    assert await store.idempotency_reserve("logical-1", "suspend", key, fingerprint) is None
    result = {"ok": True, "slot_revision": 2}
    assert (
        await store.idempotency_complete("logical-1", "suspend", key, fingerprint, result) == result
    )

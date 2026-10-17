"""Current persistent-store schema and durable request-key state transitions."""

import pytest
import pytest_asyncio

from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest_asyncio.fixture
async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    return repo.path, store


async def test_same_key_and_payload_reads_committed_result(store):
    _path, store = store
    fingerprint = store.idempotency_fingerprint({"expected_revision": 1})
    result = {"ok": True, "slot_revision": 2}
    await store.idempotency_put("logical-1", "play", "idem-0001", fingerprint, result)

    assert await store.idempotency_get("logical-1", "play", "idem-0001", fingerprint) == result


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


async def test_pending_reservation_reports_in_progress(store):
    _path, store = store
    fingerprint = store.idempotency_fingerprint({"expected_revision": 1})
    key = "idem-pending-0001"
    await store.idempotency_reserve("logical-1", "suspend", key, fingerprint)

    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_get("logical-1", "suspend", key, fingerprint)

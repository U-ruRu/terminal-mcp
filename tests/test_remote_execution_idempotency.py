"""Execution-node receipts reference authenticated global identities, not local slots."""

import sqlite3

import pytest

from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_remote_identity_receipt_needs_no_fake_local_authority_slot(tmp_path):
    repo = SqliteRepository(tmp_path / "remote.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    assert await store.get_slot("foreign-agent") is None
    args = ("foreign-agent", "command:run", "foreign-session:rpc-unique", "fingerprint")
    assert await store.idempotency_reserve(*args) is None
    with pytest.raises(PersistentStoreError, match="idempotency_in_progress"):
        await store.idempotency_reserve(*args)
    result = {"ok": True, "cmd_hash": "01234567"}
    assert await store.idempotency_complete(*args, result) == result
    assert await store.idempotency_reserve(*args) == result
    with pytest.raises(PersistentStoreError, match="idempotency_conflict"):
        await store.idempotency_get(*args[:3], "other")
    assert await store.get_slot("foreign-agent") is None
    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

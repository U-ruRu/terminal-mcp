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


@pytest.mark.asyncio
async def test_v20_execution_receipt_migration_preserves_rows_indexes_and_audit_sequence(tmp_path):
    repo = SqliteRepository(tmp_path / "upgrade.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    await store.create_slot("la_home", "Home", "ABCD", authority_node_id="home")
    await store.idempotency_reserve("la_home", "command.run", "pending", "f1")
    await store.idempotency_put("la_home", "command.run", "done", "f2", {"ok": True})
    await store.add_audit_event("la_home", "command.run", principal_id="client")
    with sqlite3.connect(repo.path) as db:
        originals = {}
        for table in ("persistent_idempotency", "persistent_agent_audit"):
            originals[table] = db.execute(f"SELECT * FROM {table}").fetchall()
            ddl = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            closing = ddl.rfind(")")
            old = table + "_old"
            ddl = (
                ddl[:closing]
                + ", FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) "
                "ON DELETE RESTRICT"
                + ddl[closing:]
            )
            ddl = ddl.replace("CREATE TABLE " + table, "CREATE TABLE " + old, 1)
            db.execute(ddl)
            db.execute(f"INSERT INTO {old} SELECT * FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {old} RENAME TO {table}")
        db.execute("CREATE INDEX audit_test_index ON persistent_agent_audit(event_type)")
        db.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='persistent_agent_audit'")
        db.execute("PRAGMA user_version=20")
        db.commit()
    await repo.initialize()
    # Repeat initialization verifies that the upgrade is idempotent.
    await repo.initialize()
    with sqlite3.connect(repo.path) as db:
        for table, before in originals.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == before
            assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []
        assert (
            db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='persistent_agent_audit'"
            ).fetchone()[0]
            == 500
        )
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_test_index'").fetchone()
        assert db.execute("PRAGMA foreign_key_list(logical_agent_work_sessions)").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    await store.idempotency_reserve("foreign-agent", "command.run", "new", "f3")
    await store.add_audit_event("foreign-agent", "command.run", principal_id="remote-client")
    assert await store.get_slot("foreign-agent") is None

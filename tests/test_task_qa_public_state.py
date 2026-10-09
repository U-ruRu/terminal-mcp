"""Six-state workflow contract, including upgrades of old SQLite CHECK tables."""
import sqlite3
import aiosqlite
import pytest
from pydantic import ValidationError

from terminal_mcp.application.task_requests import TaskStateRequest
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.mcp.role_contracts import TaskStateInput
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_public_state_accepts_qa_without_cas_or_owner_and_preserves_revision(tmp_path):
    db = tmp_path / "tasks.sqlite3"
    repo = SqliteRepository(db, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(db)
    coordinator = TaskCoordinator(store)
    created = await coordinator.mutate(
        "creator", action="create", namespace="qa-check", task_id="one",
        title="State independence", isolation_hint="none")
    assert created["ok"]
    base = (await store.get_task("qa-check", "one"))["revision"]
    claim = await coordinator.mutate(
        "creator", action="claim", namespace="qa-check", task_id="one",
        claim_intent="test")
    assert claim["ok"]
    events = len(await store.list_events("qa-check", "one"))

    for state in ("qa", "done", "blocked", "in_progress", "ready", "deferred"):
        payload = dict(namespace="qa-check", task_id="one", state=state)
        assert TaskStateInput.model_validate(payload).state == state
        request = TaskStateRequest(action="state", **payload)
        changed = await coordinator.mutate("other-agent", **request.model_dump())
        assert changed["ok"], changed
        row = await store.get_task("qa-check", "one")
        assert row["state"] == state and row["revision"] == base
    assert len(await store.list_events("qa-check", "one")) == events + 6
    repeat = await coordinator.mutate(
        "other-agent", action="state", namespace="qa-check",
        task_id="one", state="deferred")
    assert repeat["ok"]
    assert len(await store.list_events("qa-check", "one")) == events + 6
    with pytest.raises(ValidationError):
        TaskStateInput.model_validate({
            "namespace":"qa-check","task_id":"one","state":"qa",
            "expected_revision":base})
    with pytest.raises(ValidationError):
        TaskStateInput.model_validate({
            "namespace":"qa-check","task_id":"one","state":"invalid"})


@pytest.mark.asyncio
async def test_upgrade_old_state_constraint_retains_data_indexes_and_triggers(tmp_path):
    repo = SqliteRepository(tmp_path / "old.sqlite3", tmp_path / "old-output.sqlite3")
    async with aiosqlite.connect(repo.path) as db:
        await db.executescript("""
            CREATE TABLE work_items (
                namespace TEXT, task_id TEXT, state TEXT
                CHECK(state IN ('ready','in_progress','blocked','deferred','done')),
                revision INTEGER, PRIMARY KEY(namespace, task_id)
            );
            CREATE INDEX ix_demo_state ON work_items(state);
            CREATE TABLE audit_events(event TEXT NOT NULL);
            CREATE TRIGGER tr_state_update AFTER UPDATE ON work_items BEGIN
                INSERT INTO audit_events(event) VALUES(NEW.state);
            END;
            INSERT INTO work_items VALUES('original','keep','ready',7);
        """)
        await db.commit()
        assert await repo._migrate_work_items_qa_state(db) is True
        assert await repo._migrate_work_items_qa_state(db) is False
        await db.execute("UPDATE work_items SET state='qa' WHERE namespace='original'")
        await db.commit()
        row = await (await db.execute(
            "SELECT state,revision FROM work_items WHERE namespace='original'"
        )).fetchone()
        assert row == ("qa", 7)
        assert (await (await db.execute(
            "SELECT event FROM audit_events"
        )).fetchone())[0] == "qa"
        assert (await (await db.execute(
            "SELECT name FROM sqlite_master WHERE name='ix_demo_state'"
        )).fetchone())[0] == "ix_demo_state"


def test_executor_task_state_mcp_has_three_fields_and_minimal_result(tmp_path):
    from fastapi.testclient import TestClient
    from terminal_mcp.app import create_app
    from test_access_mesh_mcp_runtime import settings, call, rpc

    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        tools = rpc(client, "executor", "tools/list").json()["result"]["tools"]
        state_schema = next(item["inputSchema"] for item in tools
                            if item["name"] == "task_state")
        assert set(state_schema["properties"]) == {"namespace", "task_id", "state"}
        assert set(state_schema["required"]) == {"namespace", "task_id", "state"}
        assert "qa" in state_schema["properties"]["state"].get("enum", [])
        started = call(client, "access", "session", {"action": "start"})
        number = started["session_number"]
        assert call(client, "executor", "session", {"session_number": number}) == {"ok": True}
        assert call(client, "coordinator", "session", {"session_number": number}) == {"ok": True}
        created = call(client, "coordinator", "task_manage", {
            "action": "create", "namespace": "qa-mcp", "task_id": "one",
            "title": "Public QA state", "isolation_hint": "none",
        })
        assert created["ok"], created
        changed = call(client, "executor", "task_state", {
            "namespace": "qa-mcp", "task_id": "one", "state": "qa",
        })
        assert changed == {"ok": True}
        missing = call(client, "executor", "task_state", {
            "namespace": "qa-mcp", "task_id": "missing", "state": "qa",
        })
        assert missing["ok"] is False
        assert missing["error"]["code"] == "task_not_found"

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


@pytest.mark.asyncio
async def test_append_checkpoint_and_exact_comment_dedup_without_revision(tmp_path):
    path = tmp_path / "append.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(path)
    coordinator = TaskCoordinator(store)
    key = {"namespace": "append", "task_id": "work"}
    assert (await coordinator.mutate(
        "author", action="create", **key, title="Appends",
        isolation_hint="none",
    ))["ok"]
    rev = (await store.get_task(**key))["revision"]
    first = await coordinator.mutate(
        "author", action="comment", **key, comment_text="same text  ")
    assert first["ok"], first
    duplicate = await coordinator.mutate(
        "author", action="comment", **key, comment_text="same text  ")
    assert not duplicate["ok"] and duplicate["code"] == "duplicate_comment"
    other = await coordinator.mutate(
        "other", action="comment", **key, comment_text="same text  ")
    assert other["ok"]
    different = await coordinator.mutate(
        "author", action="comment", **key, comment_text="same text")
    assert different["ok"]
    for payload in ({"step": 1}, {"step": 1}, {"step": 2}):
        result = await coordinator.mutate(
            "other", action="checkpoint", **key, checkpoint=payload)
        assert result["ok"], result
    saved = await store.get_task(**key)
    assert saved["checkpoint"] == {"step": 2}
    assert saved["revision"] == rev
    events = await store.list_events(**key)
    assert len([x for x in events if x["event_type"] == "comment"]) == 3
    assert len([x for x in events if x["event_type"] == "checkpoint"]) == 3
    latest = await store.latest_checkpoint_event(**key)
    assert latest["payload"]["checkpoint"] == {"step": 2}


def test_mesh_task_comment_public_minimal_success_and_duplicate(tmp_path):
    from fastapi.testclient import TestClient
    from terminal_mcp.app import create_app
    from test_access_mesh_mcp_runtime import settings, call, rpc

    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        number = call(client, "access", "session", {"action":"start"})["session_number"]
        assert call(client, "executor", "session", {"session_number": number})["ok"]
        assert call(client, "coordinator", "session", {"session_number": number})["ok"]
        created = call(client, "coordinator", "task_manage", {
            "action": "create", "namespace": "comments-mcp", "task_id": "one",
            "title": "Append", "isolation_hint": "none",
        })
        assert created["ok"]
        params = {"namespace": "comments-mcp", "task_id": "one"}
        accepted = call(client, "executor", "task_comment", {
            **params, "action": "comment", "comment_text": "test text",
        })
        assert accepted == {"ok": True}
        duplicate = call(client, "executor", "task_comment", {
            **params, "action": "comment", "comment_text": "test text",
        })
        assert duplicate["ok"] is False
        assert duplicate["error"]["code"] == "duplicate_comment"
        for i in range(2):
            accepted = call(client, "executor", "task_comment", {
                **params, "action": "checkpoint", "checkpoint": {"step": i},
            })
            assert accepted == {"ok": True}
        catalog = rpc(client, "executor", "tools/list").json()["result"]["tools"]
        schema = next(x["inputSchema"] for x in catalog if x["name"] == "task_comment")
        assert "expected_revision" not in schema["properties"]


@pytest.mark.asyncio
async def test_task_autogenerated_id_duplicate_signature_is_atomic(tmp_path):
    import asyncio

    path = tmp_path / "dedupe.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    coordinator = TaskCoordinator(TaskStore(path))
    args = {
        "action": "create", "namespace": "dedupe", "title": "Same title",
        "description": "Same exact description", "isolation_hint": "none",
    }
    first, second = await asyncio.gather(
        coordinator.mutate("creator", **args),
        coordinator.mutate("creator", **args),
    )
    assert sorted([x["ok"] for x in (first, second)]) == [False, True]
    rejected = first if not first["ok"] else second
    accepted = second if second["ok"] else first
    assert rejected["code"] == "duplicate_task"
    generated_id = accepted["task"]["task_id"]
    assert generated_id.startswith("TASK-")
    assert len(generated_id) > 10
    # An explicit distinct ID always bypasses content deduplication.
    explicit = await coordinator.mutate(
        "creator", **{**args, "task_id": "explicit-other"},
    )
    assert explicit["ok"], explicit
    another_author = await coordinator.mutate("other-author", **args)
    assert another_author["ok"], another_author
    modified = await coordinator.mutate(
        "creator", **{**args, "description": "Different description"}
    )
    assert modified["ok"], modified
    duplicate_id = await coordinator.mutate(
        "creator", **{**args, "task_id": "explicit-other"}
    )
    assert not duplicate_id["ok"] and duplicate_id["code"] == "task_already_exists"


@pytest.mark.asyncio
async def test_update_cas_identical_retry_and_stale_conflicts_are_atomic(tmp_path):
    import asyncio

    path = tmp_path / "update-cas.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(path)
    coordinator = TaskCoordinator(store)
    key = {"namespace": "update-cas", "task_id": "one"}
    assert (await coordinator.mutate(
        "creator", action="create", **key, title="Initial", isolation_hint="none"
    ))["ok"]
    initial = (await store.get_task(**key))["revision"]
    args = {**key, "action": "update", "title": "Updated", "expected_revision": initial}
    first, duplicate = await asyncio.gather(
        coordinator.mutate("creator", **args),
        coordinator.mutate("creator", **args),
    )
    assert sorted([first["ok"], duplicate["ok"]]) == [False, True]
    rejected = first if not first["ok"] else duplicate
    assert rejected["code"] == "already_changed", rejected
    current = await store.get_task(**key)
    assert current["revision"] == initial + 1
    assert current["title"] == "Updated"
    repeat = await coordinator.mutate("creator", **args)
    assert not repeat["ok"] and repeat["code"] == "already_changed"
    other = await coordinator.mutate("different-agent", **args)
    assert not other["ok"] and other["code"] == "revision_conflict"
    changed_payload = await coordinator.mutate(
        "creator", **{**args, "title": "Different requested title"},
    )
    assert not changed_payload["ok"] and changed_payload["code"] == "revision_conflict"
    fresh = await coordinator.mutate(
        "creator", **{**args, "expected_revision": current["revision"],
                      "description": "New field"},
    )
    assert fresh["ok"], fresh
    assert (await store.get_task(**key))["revision"] == initial + 2

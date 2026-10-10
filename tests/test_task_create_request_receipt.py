"""A stable caller request ID must survive lost responses without a second insert."""

import asyncio

import pytest
import pytest_asyncio

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def case(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    return store, TaskCoordinator(store)


async def create(coordinator, request_id, *, title="Receipt", description="original"):
    return await coordinator.mutate(
        "writer",
        action="create",
        namespace="receipts",
        isolation_hint="isolated-receipt",
        request_id=request_id,
        title=title,
        description=description,
    )


@pytest.mark.asyncio
async def test_retry_original_request_returns_committed_id_without_second_event(case):
    store, coordinator = case
    first = await create(coordinator, "create-123")
    assert first["ok"], first
    task_id = first["task"]["task_id"]
    retry = await create(coordinator, "create-123")
    assert retry["ok"], retry
    assert retry["task"]["task_id"] == task_id
    events = await store.list_events("receipts", task_id)
    assert sum(event["event_type"] == "created" for event in events) == 1
    independent = await create(coordinator, "create-456")
    assert not independent["ok"]
    assert independent["code"] == "duplicate_task"


@pytest.mark.asyncio
async def test_request_id_replay_after_rename_and_payload_conflict(case):
    store, coordinator = case
    first = await create(coordinator, "create-789")
    task_id = first["task"]["task_id"]
    await store.update_task_mutation("receipts", task_id, title="Renamed", expected_revision=1)
    replay = await create(coordinator, "create-789")
    assert replay["ok"]
    assert replay["task"]["task_id"] == task_id
    assert replay["task"]["revision"] == first["task"]["revision"] == 1
    assert replay["task"]["title"] == first["task"]["title"] == "Receipt"
    different = await create(coordinator, "create-789", title="Different")
    assert not different["ok"]
    assert different["code"] == "request_id_conflict"


@pytest.mark.asyncio
async def test_concurrent_same_request_id_commits_once(case):
    store, coordinator = case
    left, right = await asyncio.gather(
        create(coordinator, "same-req"), create(coordinator, "same-req")
    )
    assert left["ok"] and right["ok"], (left, right)
    assert left["task"]["task_id"] == right["task"]["task_id"]
    events = await store.list_events("receipts", left["task"]["task_id"])
    assert sum(event["event_type"] == "created" for event in events) == 1


@pytest.mark.asyncio
async def test_receipt_survives_new_coordinator_instance(case):
    store, coordinator = case
    first = await create(coordinator, "after-crash")
    restarted = TaskCoordinator(TaskStore(store.path))
    replay = await create(restarted, "after-crash")
    assert replay["ok"], replay
    assert replay["task"]["task_id"] == first["task"]["task_id"]


@pytest.mark.asyncio
async def test_failure_before_commit_rolls_back_task_and_receipt(case, monkeypatch):
    store, coordinator = case
    original = store._committed_record_tx

    async def unavailable(*args, **kwargs):
        raise RuntimeError("injected storage failure before commit")

    monkeypatch.setattr(store, "_committed_record_tx", unavailable)
    failed = await create(coordinator, "retry-after-rollback")
    assert not failed["ok"], failed
    monkeypatch.setattr(store, "_committed_record_tx", original)
    success = await create(coordinator, "retry-after-rollback")
    assert success["ok"], success


def test_request_id_public_input_schema():
    from terminal_mcp.application.task_requests import TaskCreateRequest

    request = TaskCreateRequest(
        action="create",
        namespace="receipts",
        isolation_hint="qa",
        title="Task",
        request_id="client-generated-uuid-1",
    )
    assert request.request_id == "client-generated-uuid-1"


def test_live_mcp_task_manage_receipt_is_stable_across_retries(tmp_path):
    import sqlite3

    from fastapi.testclient import TestClient
    from test_access_mesh_mcp_runtime import call, settings

    from terminal_mcp.app import create_app

    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(client, "access", "session", {"action": "start"}, request_id=30)
        assert issued["ok"]
        assert call(
            client,
            "coordinator",
            "session",
            {"session_number": issued["session_number"]},
            request_id=31,
        )["ok"]
        payload = {
            "action": "create",
            "namespace": "http-receipts",
            "isolation_hint": "test isolated",
            "title": "HTTP create receipt",
            "description": "fixed contents",
            "request_id": "http-original-req-42",
        }
        original = call(client, "coordinator", "task_manage", payload, request_id=0)
        assert original["ok"], original
        # The original response is deliberately discarded: the same operation
        # comes back through MCP, with no new task or event.
        replay = call(client, "coordinator", "task_manage", payload, request_id=0)
        assert replay["ok"], replay
        assert replay["task_id"] == original["task_id"]
        with sqlite3.connect(config.database_path) as db:
            key = ("http-receipts", original["task_id"])
            assert (
                db.execute(
                    "SELECT count(*) FROM work_events "
                    "WHERE namespace=? AND task_id=? AND event_type='created'",
                    key,
                ).fetchone()[0]
                == 1
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM work_items WHERE namespace=? AND task_id=?", key
                ).fetchone()[0]
                == 1
            )
        independent = call(
            client,
            "coordinator",
            "task_manage",
            {**payload, "request_id": "http-independent-req-43"},
            request_id=0,
        )
        assert not independent["ok"], independent
        assert independent["error"]["code"] == "duplicate_task"


@pytest.mark.asyncio
async def test_lost_response_after_commit_is_reconciled_from_receipt(case, monkeypatch):
    store, coordinator = case
    original = store.create_task_mutation

    async def simulate_lost_reply(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("response lost after durable commit")

    monkeypatch.setattr(store, "create_task_mutation", simulate_lost_reply)
    first = await create(coordinator, "postcommit-lost-response")
    assert first["ok"], first
    event_rows = await store.list_events("receipts", first["task"]["task_id"])
    assert sum(row["event_type"] == "created" for row in event_rows) == 1
    monkeypatch.setattr(store, "create_task_mutation", original)
    retry = await create(coordinator, "postcommit-lost-response")
    assert retry["ok"], retry
    assert retry["task"]["task_id"] == first["task"]["task_id"]


@pytest.mark.asyncio
async def test_unreconciled_commit_error_reports_unknown_outcome(case, monkeypatch):
    store, coordinator = case

    async def fail_indeterminately(*args, **kwargs):
        raise OSError("SQLite commit acknowledgement uncertain")

    monkeypatch.setattr(store, "create_task_mutation", fail_indeterminately)
    result = await create(coordinator, "may-or-may-not-commit")
    assert not result["ok"], result
    assert result["code"] == "storage_unavailable"
    assert result["outcome"] == "unknown"

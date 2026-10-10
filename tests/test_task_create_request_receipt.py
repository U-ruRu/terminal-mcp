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

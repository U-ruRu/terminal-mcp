"""A stable caller request ID must survive lost responses without a second insert."""

import asyncio
import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def case(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    return store, TaskCoordinator(store)


async def create(
    coordinator,
    request_id,
    *,
    agent_id="writer",
    namespace="receipts",
    title="Receipt",
    description="original",
):
    return await coordinator.mutate(
        agent_id,
        _claim_owner=ClaimOwner.logical_agent(agent_id),
        action="create",
        namespace=namespace,
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


@pytest.mark.asyncio
async def test_another_agent_can_use_the_same_create_request_id(case):
    _, coordinator = case
    original = await create(coordinator, "scoped-id")

    other_agent = await create(coordinator, "scoped-id", agent_id="other-writer")

    assert other_agent["ok"], other_agent
    assert other_agent["task"]["task_id"] != original["task"]["task_id"]


@pytest.mark.asyncio
async def test_another_namespace_can_use_the_same_create_request_id(case):
    _, coordinator = case
    original = await create(coordinator, "scoped-id")

    other_namespace = await create(coordinator, "scoped-id", namespace="other-receipts")

    assert other_namespace["ok"], other_namespace
    assert other_namespace["task"]["task_id"] != original["task"]["task_id"]


@pytest.mark.asyncio
async def test_distinct_create_request_commits_a_separate_task(case):
    _, coordinator = case
    original = await create(coordinator, "scoped-id")

    distinct_operation = await create(
        coordinator, "different-id", title="Separate operation", description="separate payload"
    )

    assert distinct_operation["ok"], distinct_operation
    assert distinct_operation["task"]["task_id"] != original["task"]["task_id"]


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
async def test_failure_before_commit_rolls_back_task_and_receipt(case, monkeypatch):
    store, coordinator = case

    async def unavailable(*args, **kwargs):
        raise RuntimeError("injected storage failure before commit")

    monkeypatch.setattr(store, "_committed_record_tx", unavailable)
    failed = await create(coordinator, "retry-after-rollback")
    assert not failed["ok"], failed
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM work_items").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM persistent_idempotency").fetchone()[0] == 0


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

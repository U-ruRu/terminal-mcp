"""A committed task mutation is observed from the transaction that committed it."""

import pytest
import pytest_asyncio

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def case(tmp_path):
    repo = SqliteRepository(tmp_path / "task.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    return store, TaskCoordinator(store)


async def create(coordinator, *, task_id="one"):
    return await coordinator.mutate(
        "agent",
        action="create",
        namespace="receipt",
        task_id=task_id,
        isolation_hint="none",
        title="Committed task",
        description="Keep this description",
    )


async def claim(coordinator):
    result = await coordinator.mutate(
        "agent",
        action="claim",
        namespace="receipt",
        task_id="one",
        claim_intent="test",
    )
    assert result["ok"], result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields,expected",
    [("update", {"title": "Updated"}, "ready"), ("state", {"state": "in_progress"}, "in_progress")],
)
async def test_mutation_success_survives_postcommit_readback_and_projection_failure(
    case, monkeypatch, action, fields, expected
):
    store, coordinator = case
    await store.create_task("receipt", "one", "Original")
    method = "set_workflow_state" if action == "state" else "update_task_mutation"
    original = getattr(store, method)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("post-commit read failed")

    async def commit_then_disable_reads(*args, **kwargs):
        committed = await original(*args, **kwargs)
        monkeypatch.setattr(store, "get_task", unavailable)
        monkeypatch.setattr(coordinator, "_decorate", unavailable)
        return committed

    monkeypatch.setattr(store, method, commit_then_disable_reads)
    result = await coordinator.mutate(
        "agent",
        action=action,
        namespace="receipt",
        task_id="one",
        **fields,
    )

    assert result["ok"] is True
    assert result["task"]["state"] == expected
    assert result["task"]["revision"] == (1 if action == "state" else 2)


@pytest.mark.asyncio
async def test_receipt_keeps_its_revision_when_later_writer_commits_before_response(
    case, monkeypatch
):
    store, coordinator = case
    await store.create_task("receipt", "one", "Original")
    original = store.update_task_mutation

    async def subsequent_writer(*args, **kwargs):
        committed = await original(*args, **kwargs)
        await store.update_task("receipt", "one", title="Later writer", expected_revision=2)
        return committed

    monkeypatch.setattr(store, "update_task_mutation", subsequent_writer)
    result = await coordinator.mutate(
        "agent",
        action="update",
        namespace="receipt",
        task_id="one",
        title="This writer",
        expected_revision=1,
    )

    assert result["ok"] is True
    assert (result["task"]["revision"], result["task"]["title"]) == (2, "This writer")
    assert (await store.get_task("receipt", "one"))["revision"] == 3


@pytest.mark.asyncio
async def test_checkpoint_receipt_survives_failed_postcommit_read(case, monkeypatch):
    store, coordinator = case
    await store.create_task("receipt", "one", "Checkpoint owner")
    append_checkpoint = store.append_checkpoint
    failure_installed = False

    async def unavailable(*args, **kwargs):
        raise AssertionError("post-commit read must not replace a committed receipt")

    async def commit_then_disable_reads(*args, **kwargs):
        nonlocal failure_installed
        record = await append_checkpoint(*args, **kwargs)
        monkeypatch.setattr(store, "get_task", unavailable)
        monkeypatch.setattr(coordinator, "_decorate", unavailable)
        failure_installed = True
        return record

    monkeypatch.setattr(store, "append_checkpoint", commit_then_disable_reads)
    result = await coordinator.mutate(
        "agent",
        action="checkpoint",
        namespace="receipt",
        task_id="one",
        checkpoint={"progress": 7},
    )
    assert failure_installed
    assert result["ok"] is True
    assert (result["task"]["checkpoint"], result["task"]["revision"]) == ({"progress": 7}, 1)

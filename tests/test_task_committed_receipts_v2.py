"""A committed task mutation is observed from the transaction that committed it."""

import pytest
import pytest_asyncio

from terminal_mcp.core.task_projections import TaskRecord, project_task_receipt
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
async def test_create_storage_never_reads_back_after_commit(case, monkeypatch):
    store, _ = case

    async def unavailable(*args, **kwargs):
        raise RuntimeError("post-commit readback unavailable")

    monkeypatch.setattr(store, "get_task", unavailable)
    record = await store.create_task_mutation(
        "receipt", "one", "Created", isolation_hint="none", event_agent_id="agent"
    )
    assert record["state"] == "ready"
    assert record["revision"] == 1
    assert record.claims == []
    assert record.dependencies == []
    # Auxiliary transaction metadata is not added to the task's public mapping.
    assert "claims" not in record


@pytest.mark.asyncio
async def test_update_storage_never_reads_back_after_commit(case, monkeypatch):
    store, coordinator = case
    await create(coordinator)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("post-commit readback unavailable")

    monkeypatch.setattr(store, "get_task", unavailable)
    record = await store.update_task_mutation(
        "receipt",
        "one",
        state="done",
        result={"evidence": "committed"},
        expected_revision=1,
        release_claims_reason="task_done",
    )
    assert record["state"] == "done"
    assert record["result"] == {"evidence": "committed"}
    assert record["revision"] == 2


@pytest.mark.asyncio
async def test_create_receipt_does_not_depend_on_postcommit_projection(case, monkeypatch):
    store, coordinator = case

    async def unavailable(*args, **kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(coordinator, "_decorate", unavailable)
    result = await create(coordinator)
    assert result["ok"], result
    assert result["task"]["description"] == "Keep this description"
    assert result["task"]["revision"] == 1
    assert (await store.get_task("receipt", "one"))["state"] == "ready"
    record = TaskRecord.model_validate(result["task"])
    receipt = project_task_receipt(record, include_description=True)
    assert receipt.description == "Keep this description"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields,expected",
    [
        ("update", {"title": "Updated", "description": "Updated description"}, "ready"),
        ("state", {"state": "in_progress"}, "in_progress"),
        ("done", {"result": {"evidence": "completed"}}, "done"),
        ("archive", {"archive_note": "Superseded"}, "ready"),
        ("checkpoint", {"checkpoint": {"progress": 7}}, "ready"),
    ],
)
async def test_mutation_success_survives_postcommit_readback_and_projection_failure(
    case, monkeypatch, action, fields, expected
):
    store, coordinator = case
    await create(coordinator)
    await claim(coordinator)
    mutation_method = "set_workflow_state" if action == "state" else "update_task_mutation"
    original_update = getattr(store, mutation_method)
    original_get = store.get_task

    async def unavailable(*args, **kwargs):
        raise RuntimeError("post-commit read or projection failed")

    async def lose_readback(*args, **kwargs):
        committed = await original_update(*args, **kwargs)
        monkeypatch.setattr(store, "get_task", unavailable)
        monkeypatch.setattr(coordinator, "_decorate", unavailable)
        return committed

    monkeypatch.setattr(store, mutation_method, lose_readback)
    result = await coordinator.mutate(
        "agent",
        action=action,
        namespace="receipt",
        task_id="one",
        expected_revision=1,
        **fields,
    )
    assert result["ok"], result
    assert result["task"]["state"] == expected
    assert result["task"]["revision"] == (1 if action == "state" else 2)
    saved = await original_get("receipt", "one")
    assert saved["state"] == expected
    assert saved["revision"] == result["task"]["revision"]
    record = TaskRecord.model_validate(result["task"])
    receipt = project_task_receipt(
        record, include_description=True, include_checkpoint=True, include_result=True
    )
    assert receipt.state.value == expected
    if action in {"done", "archive"}:
        assert result["task"]["owner"] is None
        assert await store.active_claims("receipt", "one") == []
    else:
        assert result["task"]["owner"] is not None
    if action == "done":
        assert receipt.result.root == {"evidence": "completed"}


@pytest.mark.asyncio
async def test_receipt_keeps_its_revision_when_later_writer_commits_before_response(
    case, monkeypatch
):
    store, coordinator = case
    await create(coordinator)
    await claim(coordinator)
    original_update = store.update_task_mutation

    async def subsequent_writer(*args, **kwargs):
        committed = await original_update(*args, **kwargs)
        await store.update_task("receipt", "one", title="Later writer", expected_revision=2)
        return committed

    monkeypatch.setattr(store, "update_task_mutation", subsequent_writer)
    result = await coordinator.mutate(
        "agent",
        action="done",
        namespace="receipt",
        task_id="one",
        result={"summary": "original committed result"},
        expected_revision=1,
    )
    assert result["ok"], result
    assert result["task"]["revision"] == 2
    assert result["task"]["title"] == "Committed task"
    assert result["task"]["state"] == "done"
    current = await store.get_task("receipt", "one")
    assert current["revision"] == 3 and current["title"] == "Later writer"

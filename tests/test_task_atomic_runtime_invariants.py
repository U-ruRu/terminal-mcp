"""Atomic task invariants retained after eliminating composite scenarios."""

import asyncio

import pytest
from test_task_coordination import register, runtime

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_forced_claim_event_records_explicit_reason(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        actor = (await register(service, "forced audit"))["self"]["agent_id"]
        namespace = "atomic"
        dependency = {"namespace": namespace, "task_id": "OPEN"}
        created = await service.task(
            actor, action="create", **dependency, title="Prerequisite", isolation_hint="none"
        )
        assert created["ok"]
        key = {"namespace": namespace, "task_id": "BLOCKED"}
        dependent = await service.task(
            actor,
            action="create",
            **key,
            title="Dependent",
            isolation_hint="none",
            dependencies=[dependency],
        )
        assert dependent["ok"]
        reason = "Emergency dependency override"
        claimed = await service.task(
            actor,
            action="claim",
            **key,
            claim_intent="Verify audit",
            force=True,
            force_reason=reason,
        )
        assert claimed["ok"], claimed
        detail = await service.tasks(**key, show_details=True)
        matching = [
            e for e in detail["task"]["events"] if e["payload"].get("force_reason") == reason
        ]
        assert len(matching) == 1
        assert matching[0]["payload"]["blocking_dependencies"][0]["task_id"] == "OPEN"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_reentering_ready_resets_ready_since(tmp_path):
    repo = SqliteRepository(tmp_path / "task.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    key = {"namespace": "atomic", "task_id": "READY"}
    created = await coordinator.mutate(
        "actor", action="create", **key, title="Task", isolation_hint="none"
    )
    assert created["ok"]
    first = (await store.get_task(**key))["ready_since"]
    assert (await coordinator.mutate("actor", action="state", **key, state="blocked"))["ok"]
    assert (await store.get_task(**key))["ready_since"] is None
    await asyncio.sleep(0.003)
    assert (await coordinator.mutate("actor", action="state", **key, state="ready"))["ok"]
    last = (await store.get_task(**key))["ready_since"]
    assert last and last != first


@pytest.mark.asyncio
async def test_metadata_update_preserves_ready_since(tmp_path):
    repo = SqliteRepository(tmp_path / "task.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    key = {"namespace": "atomic", "task_id": "READY"}
    created = await coordinator.mutate(
        "actor", action="create", **key, title="Before", isolation_hint="none"
    )
    assert created["ok"]
    ready_since = (await store.get_task(**key))["ready_since"]
    updated = await coordinator.mutate(
        "actor",
        action="update",
        **key,
        title="After",
        expected_revision=created["task"]["revision"],
    )
    assert updated["ok"], updated
    assert (await store.get_task(**key))["ready_since"] == ready_since

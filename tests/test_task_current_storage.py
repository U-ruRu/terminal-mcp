"""Atomic task obligations from retired service-level workflow scenarios."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.task_requests import TaskCheckpointRequest
from terminal_mcp.application.tasks import TaskApplication
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskAgentBusy, TaskStore


@pytest_asyncio.fixture
async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.sqlite3")
    await repo.initialize()
    return TaskStore(repo.path)


async def dependent_task(store):
    await store.create_task("current", "parent", "Prerequisite")
    await store.create_task("current", "child", "Dependent")
    await store.set_dependencies(
        "current", "child", [{"namespace": "current", "task_id": "parent"}]
    )


@pytest.mark.asyncio
async def test_forced_claim_commits_the_explicit_dependency_override_reason(store):
    await dependent_task(store)

    result = await TaskCoordinator(store).mutate(
        "worker",
        action="claim",
        namespace="current",
        task_id="child",
        claim_intent="Emergency repair",
        force=True,
        force_reason="Restore production",
        _claim_owner=ClaimOwner.logical_agent("worker"),
    )

    assert result["ok"] is True
    overrides = [
        event
        for event in await store.list_events("current", "child")
        if event["event_type"] == "dependency_override"
    ]
    assert len(overrides) == 1
    assert overrides[0]["payload"]["force_reason"] == "Restore production"
    assert overrides[0]["payload"]["blocking_dependencies"][0]["task_id"] == "parent"


@pytest.mark.asyncio
async def test_unfinished_archived_dependency_still_blocks_claim(store):
    await dependent_task(store)
    await store.update_task("current", "parent", archived_at="2026-01-01T00:00:00+00:00")

    result = await TaskCoordinator(store).mutate(
        "worker", action="claim", namespace="current", task_id="child", claim_intent="Work"
    )

    assert result["code"] == "dependency_open"


@pytest.mark.asyncio
async def test_dependency_cycle_rejection_preserves_the_previous_edges(store):
    await dependent_task(store)

    with pytest.raises(ValueError, match="cycle"):
        await store.set_dependencies("current", "parent", [{"task_id": "child"}])

    assert await store.dependencies("current", "parent") == []


@pytest.mark.asyncio
async def test_one_logical_owner_cannot_claim_a_second_task(store):
    await store.create_task("current", "first", "First task")
    await store.create_task("current", "second", "Second task")
    owner = ClaimOwner.logical_agent("worker")
    await store.claim_owner("current", "first", owner, claim_intent="Current work")

    with pytest.raises(TaskAgentBusy) as error:
        await store.claim_owner("current", "second", owner, claim_intent="Other work")

    assert error.value.task_id == "first"


@pytest.mark.asyncio
async def test_entering_ready_records_the_transition_time(store):
    await store.create_task("current", "one", "Paused task", state="blocked")
    resumed_at = "2026-01-02T03:04:05+00:00"

    committed = await store.set_workflow_state(
        "current", "one", "ready", agent_id="worker", now=resumed_at
    )

    assert committed["ready_since"] == resumed_at
    assert committed["revision"] == 1


@pytest.mark.asyncio
async def test_checkpoint_append_preserves_the_previous_history_entry(store):
    await store.create_task("current", "one", "Work")
    await store.append_checkpoint("current", "one", agent_id="worker", checkpoint={"step": 1})

    committed = await store.append_checkpoint(
        "current", "one", agent_id="worker", checkpoint={"step": 2}
    )

    assert committed["checkpoint"] == {"step": 2}
    assert [
        event["payload"]["checkpoint"] for event in await store.list_events("current", "one")
    ] == [{"step": 2}, {"step": 1}]


@pytest.mark.asyncio
async def test_output_update_keeps_the_review_bound_to_its_original_output(store):
    original = await store.create_task("current", "one", "Work", output_refs=["artifact:old"])
    await store.upsert_reviews(
        "current",
        "one",
        output_state_id=original["output_state_id"],
        output_refs=["artifact:old"],
        dimensions=["A"],
        verdict="NON_BLOCKING",
        agent_id="reviewer",
    )

    updated = await store.update_task("current", "one", output_refs=["artifact:new"])

    reviews = await store.reviews("current", "one")
    assert reviews[0]["output_state_id"] == original["output_state_id"]
    assert reviews[0]["output_refs"] == ["artifact:old"]
    assert updated["output_state_id"] != original["output_state_id"]


@pytest.mark.asyncio
async def test_checkpoint_dispatch_does_not_reuse_the_transport_request_id():
    backend = SimpleNamespace(task=AsyncMock(return_value={"ok": True}))
    identity = {"logical_agent_id": "worker", "work_session_id": "window", "session_epoch": 1}
    gate = SimpleNamespace(identity=AsyncMock(return_value=(identity, None)))
    app = TaskApplication(SimpleNamespace(persistent=backend), gate)
    actor = ActorContext(transport="mcp", request_id="same-request", endpoint_role="coordinator")

    await app.task(
        actor,
        TaskCheckpointRequest(
            action="checkpoint", namespace="current", task_id="one", checkpoint={"step": 1}
        ),
    )

    assert backend.task.call_args.kwargs["idempotency_key"] is None

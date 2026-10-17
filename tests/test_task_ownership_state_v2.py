"""Ownership/lifecycle is independent of the explicit workflow state (Access Mesh V2)."""

from datetime import timedelta

import aiosqlite
import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "state.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return TaskStore(repo.path)


def session_lease():
    return {
        "work_session_id": "ws_v2_ownership",
        "session_epoch": 1,
        "hard_expires_at": utc_text(utc_now() + timedelta(minutes=5)),
    }


def workflow(task):
    return {
        key: task[key]
        for key in ("state", "checkpoint", "result", "state_changed_at", "ready_since")
    }


@pytest.mark.asyncio
async def test_claim_preserves_the_explicit_workflow_state(store):
    await store.create_task("v2", "state", "Explicit state", state="ready", checkpoint={"step": 7})
    before = workflow(await store.get_task("v2", "state"))

    await store.claim_owner("v2", "state", ClaimOwner.logical_agent("la_v2"))

    assert workflow(await store.get_task("v2", "state")) == before


@pytest.mark.asyncio
async def test_release_preserves_the_explicit_workflow_state(store):
    await store.create_task(
        "v2", "state", "Completed work", state="done", result={"evidence": "keep"}
    )
    owner = ClaimOwner.logical_agent("la_v2")
    await store.claim_owner("v2", "state", owner, lease=session_lease())
    before = workflow(await store.get_task("v2", "state"))

    await store.release_owner_claim_mutation("v2", "state", owner, reason="handoff")

    assert workflow(await store.get_task("v2", "state")) == before


@pytest.mark.asyncio
async def test_delayed_owner_sweep_does_not_release_successor_claim(store):
    await store.create_task("v2", "state", "Successor", state="blocked")
    owner = ClaimOwner.logical_agent("la_v2")
    original = await store.claim_owner("v2", "state", owner)
    assert await store.release_owner_claim_mutation("v2", "state", owner, reason="end")
    successor = await store.claim_owner("v2", "state", owner)
    assert original["id"] != successor["id"]
    assert not await store.release_owner_claim_mutation(
        "v2", "state", owner, reason="late sweep", expected_claim_id=original["id"]
    )
    assert (await store.active_claims("v2", "state"))[0]["id"] == successor["id"]
    assert (await store.get_task("v2", "state"))["state"] == "blocked"


@pytest.mark.asyncio
async def test_owner_sweep_isolates_one_claim_audit_failure(store):
    owner = ClaimOwner.logical_agent("la_v2")
    for task_id in ("bad", "good"):
        await store.create_task("v2", task_id, task_id, state="in_progress", checkpoint="keep")
    # Seed independently held claims so one cleanup failure cannot starve the next.
    async with aiosqlite.connect(store.path) as db:
        for task_id in ("bad", "good"):
            await db.execute(
                "INSERT INTO work_claims(namespace,task_id,agent_id,claimed_at,"
                "owner_kind,owner_id) "
                "VALUES(?,?,?,?,?,?)",
                ("v2", task_id, owner.owner_id, utc_text(), owner.kind, owner.owner_id),
            )
        await db.execute(
            "CREATE TRIGGER fail_bad_claim_audit BEFORE INSERT ON work_events "
            "WHEN NEW.task_id='bad' AND NEW.event_type='claim_released' "
            "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
        )
        await db.commit()
    result = await store.release_owner_claims_mutation(owner=owner, reason="slot_deleted")
    assert result["ok"] is False
    assert result["released_count"] == 1
    assert [error["task_id"] for error in result["errors"]] == ["bad"]
    assert len(await store.active_claims("v2", "bad")) == 1
    assert await store.active_claims("v2", "good") == []


@pytest.mark.asyncio
async def test_content_update_preserves_nondefault_workflow_state(store):
    await store.create_task("v2", "state", "Original", state="qa")
    result = await TaskCoordinator(store).mutate(
        "agent",
        action="update",
        namespace="v2",
        task_id="state",
        title="Changed title",
        expected_revision=1,
    )
    assert result["ok"] is True
    current = await store.get_task("v2", "state")
    assert (current["title"], current["state"], current["revision"]) == (
        "Changed title",
        "qa",
        2,
    )

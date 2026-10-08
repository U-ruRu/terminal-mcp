"""Ownership/lifecycle is independent of the explicit workflow state (Access Mesh V2)."""

import json
from datetime import timedelta

import aiosqlite
import pytest
import pytest_asyncio
from pydantic import TypeAdapter, ValidationError

from terminal_mcp.application.task_requests import TaskRequest
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
@pytest.mark.parametrize("state", ["ready", "in_progress", "blocked", "deferred", "done"])
@pytest.mark.parametrize("leased", [False, True])
async def test_claim_and_release_preserve_all_explicit_workflow_fields(store, state, leased):
    await store.create_task(
        "v2",
        "state",
        "Explicit state",
        state=state,
        checkpoint={"step": 7},
        result={"evidence": "retain"},
    )
    before = workflow(await store.get_task("v2", "state"))
    owner = ClaimOwner.logical_agent("la_v2")
    await store.claim_owner("v2", "state", owner, lease=session_lease() if leased else None)
    assert workflow(await store.get_task("v2", "state")) == before
    released = await store.release_owner_claim_mutation("v2", "state", owner, reason="handoff")
    assert released is True
    assert workflow(await store.get_task("v2", "state")) == before
    after = await store.get_task("v2", "state")
    assert not await store.release_owner_claim_mutation("v2", "state", owner, reason="retry")
    assert await store.get_task("v2", "state") == after


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expired", "interrupted", "slot_suspended", "slot_deleted"])
@pytest.mark.parametrize("state", ["ready", "in_progress", "blocked", "deferred", "done"])
async def test_session_cleanup_preserves_state_even_with_old_automatic_provenance(
    store, state, reason
):
    await store.create_task(
        "v2",
        "state",
        "Old durable row",
        state=state,
        checkpoint={"step": 8},
        result="keep evidence",
    )
    owner = ClaimOwner.logical_agent("la_v2")
    fields = session_lease()
    await store.claim_owner("v2", "state", owner, lease=fields)
    # Simulate the provenance marker written by an older deployed binary.
    async with aiosqlite.connect(store.path) as db:
        await db.execute(
            "INSERT INTO work_claim_auto_state(namespace,task_id,created_at) VALUES(?,?,?)",
            ("v2", "state", utc_text()),
        )
        await db.commit()
    before = workflow(await store.get_task("v2", "state"))
    args = ("la_v2", fields["work_session_id"], fields["session_epoch"])
    assert await store.release_work_session_claims(*args, reason=reason) == 1
    assert workflow(await store.get_task("v2", "state")) == before
    after = await store.get_task("v2", "state")
    assert await store.release_work_session_claims(*args, reason=reason) == 0
    assert await store.get_task("v2", "state") == after
    async with aiosqlite.connect(store.path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM work_claim_auto_state")).fetchone())[
            0
        ] == 0


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
async def test_owner_sweep_isolates_failures_and_retries_with_atomic_audit(store):
    owner = ClaimOwner.logical_agent("la_v2")
    for task_id in ("bad", "good"):
        await store.create_task("v2", task_id, task_id, state="in_progress", checkpoint="keep")
    # Imported pre-WIP state can contain several live claims for one owner.
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
    for task_id in ("bad", "good"):
        assert (await store.get_task("v2", task_id))["state"] == "in_progress"
        assert (await store.get_task("v2", task_id))["checkpoint"] == "keep"
    async with aiosqlite.connect(store.path) as db:
        assert (
            await (
                await db.execute("SELECT COUNT(*) FROM work_events WHERE task_id='bad'")
            ).fetchone()
        )[0] == 0
        await db.execute("DROP TRIGGER fail_bad_claim_audit")
        await db.commit()
    retry = await store.release_owner_claims_mutation(owner=owner, reason="slot_deleted")
    assert retry == {"ok": True, "released_count": 1, "errors": []}
    assert await store.release_owner_claims_mutation(owner=owner, reason="slot_deleted") == {
        "ok": True,
        "released_count": 0,
        "errors": [],
    }
    async with aiosqlite.connect(store.path) as db:
        rows = await (
            await db.execute(
                "SELECT task_id,payload_json FROM work_events WHERE event_type='claim_released'"
            )
        ).fetchall()
    assert sorted(row[0] for row in rows) == ["bad", "good"]
    assert all(json.loads(row[1])["reason"] == "slot_deleted" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "in_progress", "blocked", "deferred", "done"])
async def test_property_update_preserves_state_and_only_explicit_state_can_change_it(store, state):
    await store.create_task("v2", "state", "Original", state=state)
    await store.claim("v2", "state", "agent", claim_intent="owned")
    coordinator = TaskCoordinator(store)
    updated = await coordinator.mutate(
        "agent",
        action="update",
        namespace="v2",
        task_id="state",
        title="Changed title",
        next_action="Next step",
    )
    assert updated["ok"], updated
    assert updated["task"]["state"] == state
    before = await store.get_task("v2", "state")
    denied = await coordinator.mutate(
        "agent",
        action="update",
        namespace="v2",
        task_id="state",
        state="deferred",
    )
    assert denied["ok"] is False
    assert denied["code"] == "input_validation_failed"
    assert await store.get_task("v2", "state") == before
    changed = await coordinator.mutate(
        "agent",
        action="state",
        namespace="v2",
        task_id="state",
        state="deferred",
    )
    assert changed["ok"], changed
    assert changed["task"]["state"] == "deferred"


@pytest.mark.parametrize("state", ["ready", "in_progress", "blocked", "deferred", "done", None])
def test_update_runtime_model_rejects_state_field(state):
    adapter = TypeAdapter(TaskRequest)
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python(
            {
                "action": "update",
                "namespace": "v2",
                "task_id": "state",
                "state": state,
            }
        )
    assert any(item["type"] == "extra_forbidden" for item in exc.value.errors())

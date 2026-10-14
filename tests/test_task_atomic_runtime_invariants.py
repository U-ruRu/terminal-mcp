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


@pytest.mark.asyncio
async def test_unfinished_archived_dependency_blocks_claim(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        actor = (await register(service, "archived dependency"))["self"]["agent_id"]
        prerequisite = {"namespace": "atomic", "task_id": "ARCHIVED"}
        created = await service.task(
            actor, action="create", **prerequisite, title="Prerequisite", isolation_hint="none"
        )
        assert created["ok"]
        archived = await service.task(
            actor, action="archive", **prerequisite, archive_note="Work discontinued"
        )
        assert archived["ok"]
        dependent = {"namespace": "atomic", "task_id": "CHILD"}
        assert (
            await service.task(
                actor,
                action="create",
                **dependent,
                title="Dependent",
                dependencies=[prerequisite],
                isolation_hint="none",
            )
        )["ok"]
        claim = await service.task(
            actor, action="claim", **dependent, claim_intent="Check dependency"
        )
        assert claim["ok"] is False
        assert claim["code"] == "dependency_open"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_cycle_update_rejected_without_partial_dependency_change(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        actor = (await register(service, "cycle validation"))["self"]["agent_id"]
        a = {"namespace": "atomic", "task_id": "A"}
        b = {"namespace": "atomic", "task_id": "B"}
        for key in (a, b):
            assert (
                await service.task(
                    actor, action="create", **key, title=key["task_id"], isolation_hint="none"
                )
            )["ok"]
        first = await service.task(actor, action="update", **a, dependencies=[b])
        assert first["ok"]
        cycle = await service.task(actor, action="update", **b, dependencies=[a])
        assert cycle["ok"] is False
        details = await service.tasks(**b, show_details=True)
        assert details["task"]["dependencies"] == []
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_completion_creates_parent_feedback_with_candidate_result(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        actor = (await register(service, "review success"))["self"]["agent_id"]
        base = {"namespace": "atomic"}
        implementation = {"task_id": "IMPL", **base}
        review = {"task_id": "REVIEW", **base}
        assert (
            await service.task(
                actor,
                action="create",
                **implementation,
                title="Parent",
                candidate_ref="candidate-1",
                isolation_hint="none",
            )
        )["ok"]
        assert (
            await service.task(
                actor,
                action="create",
                **review,
                title="Review",
                lane="review",
                candidate_ref="candidate-1",
                isolation_hint="none",
            )
        )["ok"]
        assert (
            await service.task(
                actor,
                action="relate",
                **review,
                relation_kind="review_of",
                related_namespace="atomic",
                related_task_id="IMPL",
            )
        )["ok"]
        assert (
            await service.task(
                actor,
                action="claim",
                **review,
                claim_intent="Review candidate",
            )
        )["ok"]
        verdict = {"verdict": "approved", "candidate": "candidate-1"}
        assert (await service.task(actor, action="done", **review, result=verdict))["ok"]
        detail = await service.tasks(**implementation, show_details=True)
        feedback = [
            event
            for event in detail["task"]["events"]
            if event["event_type"] == "review_feedback"
            and event["payload"].get("outcome") == "done"
        ]
        assert len(feedback) == 1
        assert feedback[0]["payload"]["result"] == verdict
    finally:
        await terminal.stop()

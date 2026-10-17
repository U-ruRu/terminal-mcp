"""The current mesh backend returns the snapshot committed by its mutation."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from native_runtime_helpers import native_agent

from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import AccessSlotEvent, SlotPolicy
from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class Backend(PersistentBackend):
    def __init__(self, tasks, native):
        self.lifecycle = SimpleNamespace(operation_guard=self._operation_guard)
        self.service = SimpleNamespace(
            events=None, access_mesh=SimpleNamespace(store=native, clock=native.clock)
        )
        self.task_coordinator = TaskCoordinator(tasks)
        self.task_store = tasks

    @asynccontextmanager
    async def _operation_guard(self, logical_agent_id):
        yield

    async def _execution_authority(self, *args, **kwargs):
        # Admission is already granted; this test owns only the committed receipt.
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_claim_receipt_retains_committed_revision_under_a_later_writer(tmp_path, monkeypatch):
    repo = SqliteRepository(tmp_path / "policy.db")
    await repo.initialize()
    tasks = TaskStore(repo.path)
    await tasks.create_task("policy", "task", "Original", state="ready")
    native, _ = native_agent(repo.path, "la_policy", session_id="work_policy")
    runtime = Backend(tasks, native)
    original = runtime.task_coordinator.mutate

    async def later_writer(*args, **kwargs):
        result = await original(*args, **kwargs)
        await tasks.update_task("policy", "task", title="Later writer", state="blocked")
        return result

    monkeypatch.setattr(runtime.task_coordinator, "mutate", later_writer)
    result = await runtime.task(
        logical_agent_id="la_policy",
        work_session_id="work_policy",
        session_epoch=1,
        action="claim",
        namespace="policy",
        task_id="task",
        claim_intent="verify public claim",
    )

    assert result["ok"], result
    assert {field: result["task"][field] for field in ("revision", "state", "title")} == {
        "revision": 1,
        "state": "ready",
        "title": "Original",
    }
    assert (await tasks.get_task("policy", "task"))["revision"] == 2


async def end_and_cleanup(native, tasks, identity):
    slot = native.slot_for_agent(identity["logical_agent_id"])
    number_cycle = native.number_cycle(slot, native.clock())
    native.apply_event(
        AccessSlotEvent(
            issuer_id=slot.issuer_id,
            slot_id=slot.slot_id,
            logical_agent_id=slot.logical_agent_id,
            revision=slot.revision + 1,
            event_id="end-before-task-commit",
            kind="SessionEnded",
            effective_at=native.clock(),
        ),
        authenticated_peer_id=native.local_node_id,
        mutation_end=number_cycle,
    )

    async def revoke_session(*args, **kwargs):
        return []

    mesh = AccessMeshApplication(
        native,
        task_store=tasks,
        execution_fence=SimpleNamespace(revoke_session=revoke_session),
        clock=native.clock,
    )
    await mesh.cleanup()
    assert native.pending_cleanup() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["end", "move"])
async def test_late_claim_cannot_commit_after_completed_native_cleanup(
    tmp_path, monkeypatch, transition
):
    repo = SqliteRepository(tmp_path / "late-claim.db")
    await repo.initialize()
    tasks = TaskStore(repo.path)
    await tasks.create_task("scope", "task", "Keep this task")
    native, identity = native_agent(repo.path, policy=SlotPolicy(180, 60, release_on_end=False))
    runtime = Backend(tasks, native)
    reached_write = asyncio.Event()
    resume_write = asyncio.Event()
    original = tasks.claim_owner

    async def paused_claim(*args, **kwargs):
        reached_write.set()
        await resume_write.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(tasks, "claim_owner", paused_claim)
    before = await tasks.get_task("scope", "task")
    pending = asyncio.create_task(
        runtime.task(
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            action="claim",
            namespace="scope",
            task_id="task",
            claim_intent="Must not appear after cleanup",
        )
    )
    await asyncio.wait_for(reached_write.wait(), timeout=2)
    try:
        await end_and_cleanup(native, tasks, identity)
        if transition == "move":
            native.begin_topology_transition("move-before-task-commit")
            native.finish_topology_transition(
                "move-before-task-commit",
                namespace_id="mesh-destination",
                authority_id=native.local_node_id,
                mesh_id="mesh-destination",
            )
    finally:
        resume_write.set()
        result = await pending
    assert result == {
        "ok": False,
        "code": "mesh_namespace_mismatch" if transition == "move" else "session_not_active",
        "error": "mesh_namespace_mismatch" if transition == "move" else "session_not_active",
        "outcome": "not_committed",
    }
    assert await tasks.active_claims("scope", "task") == []
    assert await tasks.get_task("scope", "task") == before
    with native._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM work_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_native_write_admission_preserves_durable_claim_policy(tmp_path):
    repo = SqliteRepository(tmp_path / "durable-claim.db")
    await repo.initialize()
    tasks = TaskStore(repo.path)
    await tasks.create_task("scope", "task", "Keep this task")
    native, identity = native_agent(repo.path, policy=SlotPolicy(180, 60, release_on_end=False))
    runtime = Backend(tasks, native)
    result = await runtime.task(
        logical_agent_id=identity["logical_agent_id"],
        work_session_id=identity["work_session_id"],
        session_epoch=identity["session_epoch"],
        action="claim",
        namespace="scope",
        task_id="task",
        claim_intent="Durable ownership",
    )
    assert result["ok"], result
    claims = await tasks.active_claims("scope", "task")
    assert len(claims) == 1
    await end_and_cleanup(native, tasks, identity)
    assert await tasks.active_claims("scope", "task") == claims
    with native._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM work_claim_leases").fetchone()[0] == 0

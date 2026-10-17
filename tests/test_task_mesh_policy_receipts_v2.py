"""The current mesh backend returns the snapshot committed by its mutation."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


class Backend(PersistentBackend):
    def __init__(self, tasks):
        self.lifecycle = SimpleNamespace(operation_guard=self._operation_guard)
        self.service = SimpleNamespace(events=None, access_mesh=object())
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
    runtime = Backend(tasks)
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

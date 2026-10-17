"""LIVE-021: diagnostic failures cannot replace a known committed result."""

from types import SimpleNamespace

import pytest

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.telemetry import current_tool, current_transport, observed


class BrokenEvents:
    def emit(self, *args, **kwargs):
        raise RuntimeError("injected diagnostic sink failure")


class BrokenMetrics:
    def inc(self, name, *args, **kwargs):
        if name == "terminal_mcp_requests_total":
            raise RuntimeError("injected completion metric failure")

    def observe(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", ["events", "metrics"])
async def test_diagnostics_preserve_committed_update_and_context(tmp_path, sink):
    repo = SqliteRepository(tmp_path / "task.db", tmp_path / "output.db")
    await repo.initialize()
    store = TaskStore(repo.path)
    tasks = TaskCoordinator(store)
    key = {"namespace": "live021", "task_id": "safe-task"}
    await tasks.mutate("author", action="create", **key, title="Safe", isolation_hint="none")
    service = SimpleNamespace(
        events=BrokenEvents() if sink == "events" else None,
        metrics=BrokenMetrics() if sink == "metrics" else None,
    )
    before = (current_tool.get(), current_transport.get())
    result = await observed(
        service,
        "rest",
        "task",
        tasks.mutate(
            "author", action="update", **key, description="CONTENT-A-safe", expected_revision=1
        ),
    )
    saved = await store.get_task(**key)
    assert saved["description"] == "CONTENT-A-safe" and saved["revision"] == 2
    assert result["ok"] and result["task"]["revision"] == 2
    assert (current_tool.get(), current_transport.get()) == before

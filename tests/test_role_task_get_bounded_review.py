"""History paging budgets the selected stream, not the full task record."""

from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.observations import ObservationApplication
from terminal_mcp.mcp.role_contracts import TaskGetInput, TaskGraphInput
from terminal_mcp.mcp.roles import _task_get, _task_graph


class _Gate:
    async def resolve(self, *_args):
        return SimpleNamespace(failure=None)


class _Tasks:
    def __init__(self):
        self.record = {
            "namespace": "research",
            "task_id": "LARGE",
            "title": "Large task",
            "lane": "implementation",
            "priority": "P1",
            "state": "in_progress",
            "operational_status": "in_progress",
            "revision": 1,
            "events": [
                {
                    "id": n,
                    "event_type": "comment",
                    "payload": {"text": "x" * 170},
                    "created_at": "2026-10-07T21:00:00Z",
                }
                for n in range(350)
            ],
        }

    async def tasks(self, **_kwargs):
        return {"ok": True, "task": self.record}


@pytest.mark.asyncio
async def test_coordinator_large_task_history_is_paginated_before_wire_budget():
    app = ObservationApplication(_Tasks(), _Gate())

    history = await _task_get(
        app,
        ActorContext(endpoint_role="coordinator"),
        TaskGetInput(
            namespace="research", task_id="LARGE", detail="history", history_kind="events", limit=1
        ),
    )

    assert history["ok"] is True
    assert len(history["task"]["items"]) == 1
    assert history["task"]["next_cursor"] is not None


@pytest.mark.asyncio
async def test_coordinator_large_task_graph_ignores_unrelated_history_size():
    app = ObservationApplication(_Tasks(), _Gate())
    actor = ActorContext(endpoint_role="coordinator")
    graph = await _task_graph(
        app, actor, TaskGraphInput(namespace="research", task_id="LARGE", limit=1)
    )
    assert graph["ok"] is True, graph
    assert graph["nodes"] == [{"namespace": "research", "task_id": "LARGE", "state": "in_progress"}]
    assert graph["edges"] == []

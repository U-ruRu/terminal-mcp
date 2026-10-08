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
    actor = ActorContext(endpoint_role="coordinator")
    full = await app.observe(
        actor,
        subject="tasks",
        namespace="research",
        task_id="LARGE",
        detail="full",
        show_done=True,
        show_archived=True,
        limit=1,
    )
    assert full["ok"] is False and full["code"] == "output_item_too_large"

    history = await _task_get(
        app,
        actor,
        TaskGetInput(
            namespace="research", task_id="LARGE", detail="history", history_kind="events", limit=1
        ),
    )
    assert history["ok"] is True, history
    assert history["task"]["kind"] == "events"
    assert len(history["task"]["items"]) == 1
    assert history["task"]["next_cursor"] is not None

    next_page = await _task_get(
        app,
        actor,
        TaskGetInput(
            namespace="research",
            task_id="LARGE",
            detail="history",
            history_kind="events",
            limit=1,
            cursor=history["task"]["next_cursor"],
        ),
    )
    assert next_page["ok"] is True
    assert next_page["task"]["items"][0]["id"] == 1

    current = await _task_get(
        app, actor, TaskGetInput(namespace="research", task_id="LARGE", detail="detail")
    )
    assert current["ok"] is True
    assert current["task"]["task_id"] == "LARGE"


@pytest.mark.asyncio
async def test_executor_cannot_defer_full_task_budget():
    app = ObservationApplication(_Tasks(), _Gate())
    actor = ActorContext(endpoint_role="executor")
    response = await app.observe(
        actor,
        subject="tasks",
        namespace="research",
        task_id="LARGE",
        detail="full",
        _defer_full_task_budget=True,
    )
    assert response["ok"] is False
    assert response["code"] == "capability_not_allowed"


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

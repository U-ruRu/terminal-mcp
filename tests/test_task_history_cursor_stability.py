"""Regression contract for coordinator task history across concurrent mutations."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.observations import ObservationApplication
from terminal_mcp.mcp.role_contracts import TaskGetInput, TaskListInput
from terminal_mcp.mcp.roles import _task_get, _task_list


class _Gate:
    async def resolve(self, *_args):
        return SimpleNamespace(failure=None)


def _event(event_id: int, kind: str = "comment") -> dict:
    return {
        "id": event_id,
        "event_type": kind,
        "payload": {"text": f"event-{event_id}"},
        "created_at": "2026-10-10T20:00:00Z",
    }


class _Tasks:
    def __init__(self, events=None):
        self.record = {
            "namespace": "qa",
            "task_id": "CURSOR",
            "title": "Cursor mutation contract",
            "lane": "review",
            "priority": "P2",
            "state": "in_progress",
            "operational_status": "in_progress",
            "revision": 3,
            "events": deepcopy(
                events if events is not None else [_event(i) for i in range(5, 0, -1)]
            ),
        }

    async def tasks(self, **_kwargs):
        return {"ok": True, "task": deepcopy(self.record)}


@pytest.fixture
def history_app():
    backend = _Tasks()
    return backend, ObservationApplication(backend, _Gate())


async def _page(app, *, cursor=None, kind="events", limit=2):
    return await _task_get(
        app,
        ActorContext(endpoint_role="coordinator"),
        TaskGetInput(
            namespace="qa",
            task_id="CURSOR",
            detail="history",
            history_kind=kind,
            limit=limit,
            cursor=cursor,
        ),
    )


@pytest.mark.asyncio
async def test_history_insertion_invalidates_old_offset_cursor(history_app):
    backend, app = history_app
    first = await _page(app)
    cursor = first["task"]["next_cursor"]
    backend.record["events"].insert(0, _event(6, "state"))
    subsequent = await _page(app, cursor=cursor)
    assert subsequent["ok"] is False, subsequent
    assert subsequent["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_history_same_cursor_wrong_stream_is_rejected(history_app):
    _backend, app = history_app
    first = await _page(app)
    response = await _page(app, cursor=first["task"]["next_cursor"], kind="comments")
    assert response["ok"] is False
    assert response["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_task_list_filters_contract_is_forwarded():
    from unittest.mock import AsyncMock

    app = SimpleNamespace(
        observe=AsyncMock(return_value={"ok": True, "tasks": [], "next_cursor": None})
    )
    actor = ActorContext(endpoint_role="coordinator")

    await _task_list(
        app, actor, TaskListInput(namespace="qa", state="done", lane="review", tags=["cursor"])
    )

    call = app.observe.call_args.kwargs
    assert {
        key: call[key]
        for key in ("namespace", "state", "lane", "tags", "show_done", "show_archived")
    } == {
        "namespace": "qa",
        "state": "done",
        "lane": "review",
        "tags": ["cursor"],
        "show_done": True,
        "show_archived": False,
    }


@pytest.mark.asyncio
async def test_sqlite_task_list_applies_the_requested_filter_combination(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository
    from terminal_mcp.storage.tasks import TaskStore

    repo = SqliteRepository(tmp_path / "list.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    await store.create_task("qa", "ACTIVE", "Active", lane="review", tags=["relevant"])
    await store.create_task(
        "qa", "DONE", "Completed", lane="review", state="done", tags=["relevant"]
    )
    await store.create_task(
        "qa", "ARCHIVED", "Archived", lane="review", state="done", tags=["relevant"]
    )
    await store.update_task("qa", "ARCHIVED", archived_at="2026-10-10T20:00:00Z")
    await store.create_task(
        "other", "OTHER", "Different namespace", lane="review", state="done", tags=["relevant"]
    )
    await store.create_task("qa", "UNTAGGED", "Without tag", lane="review", state="done")

    page = await store.list_tasks(
        namespace="qa", lane="review", state="done", tags=["relevant"], show_done=True
    )

    assert [task["task_id"] for task in page] == ["DONE"]


@pytest.mark.asyncio
async def test_history_unrelated_metadata_and_other_stream_dont_invalidate(history_app):
    backend, app = history_app
    backend.record["comments"] = deepcopy(backend.record["events"])
    first = await _page(app, kind="comments", limit=2)
    backend.record["title"] = "Changed task title"
    backend.record["events"].insert(0, _event(6, kind="state"))
    still_valid = await _page(app, kind="comments", cursor=first["task"]["next_cursor"])
    assert still_valid["ok"] is True
    assert [event["id"] for event in still_valid["task"]["items"]] == [3, 2]

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
async def test_history_stable_cursor_is_complete_sorted_and_unique(history_app):
    _backend, app = history_app
    cursor = None
    pages = []
    while True:
        response = await _page(app, cursor=cursor)
        assert response["ok"] is True, response
        rows = response["task"]["items"]
        pages.extend(item["id"] for item in rows)
        cursor = response["task"].get("next_cursor")
        if cursor is None:
            break
        assert len(pages) <= 5
    assert pages == [5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_history_insertion_invalidates_old_offset_cursor(history_app):
    backend, app = history_app
    first = await _page(app)
    assert [row["id"] for row in first["task"]["items"]] == [5, 4]
    cursor = first["task"]["next_cursor"]
    backend.record["events"].insert(0, _event(6, "state"))
    backend.record["state"] = "done"
    # State transition is not a content-revision bump.
    assert backend.record["revision"] == 3
    subsequent = await _page(app, cursor=cursor)
    assert subsequent["ok"] is False, subsequent
    assert subsequent["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_history_existing_event_edit_invalidates_cursor(history_app):
    backend, app = history_app
    first = await _page(app)
    backend.record["events"][-1]["payload"]["text"] = "modified"
    subsequent = await _page(app, cursor=first["task"]["next_cursor"])
    assert subsequent["ok"] is False
    assert subsequent["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_history_same_cursor_wrong_stream_is_rejected(history_app):
    _backend, app = history_app
    first = await _page(app)
    response = await _page(app, cursor=first["task"]["next_cursor"], kind="comments")
    assert response["ok"] is False
    assert response["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_history_empty_and_single_last_page():
    backend = _Tasks([])
    app = ObservationApplication(backend, _Gate())
    empty = await _page(app, limit=1)
    assert empty["ok"] is True
    assert empty["task"]["items"] == []
    assert empty["task"].get("next_cursor") is None
    backend.record["events"].append(_event(1))
    final = await _page(app, limit=1)
    assert final["ok"] is True
    assert [item["id"] for item in final["task"]["items"]] == [1]
    assert final["task"].get("next_cursor") is None


@pytest.mark.asyncio
async def test_task_list_filters_contract_is_forwarded():
    class Application:
        last_call = None

        async def observe(self, _actor, **kwargs):
            self.last_call = kwargs
            return {"ok": True, "tasks": [], "next_cursor": None}

    app = Application()
    actor = ActorContext(endpoint_role="coordinator")
    default = await _task_list(app, actor, TaskListInput())
    assert default["ok"] is True
    assert app.last_call["state"] is None
    assert app.last_call["namespace"] is None
    assert app.last_call["show_done"] is False
    assert app.last_call["show_archived"] is False

    done = await _task_list(
        app, actor, TaskListInput(namespace="qa", state="done", lane="review", tags=["cursor"])
    )
    assert done["ok"] is True
    assert app.last_call["show_done"] is True
    assert app.last_call["show_archived"] is False
    assert app.last_call["namespace"] == "qa"
    assert app.last_call["lane"] == "review"
    assert app.last_call["tags"] == ["cursor"]


@pytest.mark.asyncio
async def test_sqlite_history_over_one_hundred_is_fully_pageable(tmp_path):
    """Real storage read-path must not silently return only its newest 100 events."""
    from terminal_mcp.core.tasks import TaskCoordinator
    from terminal_mcp.storage.sqlite import SqliteRepository
    from terminal_mcp.storage.tasks import TaskStore

    repo = SqliteRepository(tmp_path / "history.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    await store.create_task("qa", "CURSOR", "Long history", lane="review")
    for n in range(115):
        await store.add_event("qa", "CURSOR", "comment", payload={"text": f"event-{n}"})

    class _Backend:
        def __init__(self):
            self.coordinator = TaskCoordinator(store)

        async def tasks(self, **kwargs):
            return await self.coordinator.list(**kwargs)

    app = ObservationApplication(_Backend(), _Gate())
    seen = []
    cursor = None
    while True:
        page = await _page(app, cursor=cursor, limit=17)
        assert page["ok"] is True, page
        seen.extend(item["id"] for item in page["task"]["items"])
        cursor = page["task"].get("next_cursor")
        if cursor is None:
            break

    # Events can include an implicit creation event; the explicit ones must all appear.
    stored = await store.list_events("qa", "CURSOR", limit=1000)
    assert seen == [item["id"] for item in stored]
    assert len(seen) >= 115
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_sqlite_task_list_state_archived_namespace_and_tags(tmp_path):
    from terminal_mcp.core.tasks import TaskCoordinator
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
        "qa",
        "ARCHIVED",
        "Archived completed",
        lane="review",
        state="done",
        tags=["relevant"],
    )
    await store.update_task(
        "qa", "ARCHIVED", archived_at="2026-10-10T20:00:00Z", archive_note="test"
    )
    await store.create_task("other", "REMOTE", "Different namespace", lane="implementation")

    class _Backend:
        async def tasks(self, **kwargs):
            return await TaskCoordinator(store).list(**kwargs)

    app = ObservationApplication(_Backend(), _Gate())
    actor = ActorContext(endpoint_role="coordinator")

    async def ids(**kwargs):
        page = await _task_list(app, actor, TaskListInput(**kwargs))
        assert page["ok"] is True, page
        return {(item["namespace"], item["task_id"]) for item in page["tasks"]}

    assert await ids() == {("qa", "ACTIVE"), ("other", "REMOTE")}
    assert await ids(namespace="qa") == {("qa", "ACTIVE")}
    assert await ids(namespace="qa", state="done") == {("qa", "DONE")}
    assert await ids(namespace="qa", state="done", tags=["missing"]) == set()
    assert await ids(namespace="qa", lane="implementation") == set()
    assert await ids(namespace="qa", lane="review", tags=["relevant"]) == {("qa", "ACTIVE")}


@pytest.mark.asyncio
async def test_history_stale_cursor_restart_recovers_all_events(history_app):
    backend, app = history_app
    first = await _page(app, limit=1)
    backend.record["events"].insert(0, _event(6))
    old = await _page(app, cursor=first["task"]["next_cursor"], limit=1)
    assert old["ok"] is False and old["code"] == "invalid_cursor"
    fresh = await _page(app, limit=100)
    assert fresh["ok"] is True
    assert [e["id"] for e in fresh["task"]["items"]] == [6, 5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_history_outside_result_set_rejects_corrupt_offset(history_app):
    import base64
    import json

    _backend, app = history_app
    first = await _page(app, limit=2)
    cursor = first["task"]["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload["o"] = 9000
    corrupted = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    response = await _page(app, cursor=corrupted)
    assert response["ok"] is False
    assert response["code"] == "invalid_cursor"


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


@pytest.mark.asyncio
async def test_history_review_stream_change_rejects_old_cursor(history_app):
    backend, app = history_app
    backend.record["reviews"] = [
        {
            "dimension": dimension,
            "verdict": "NON_BLOCKING",
            "evidence": {},
            "warnings": [],
            "reviewed_at": f"2026-10-10T20:00:0{index}Z",
            "reviewer": "QA",
        }
        for index, dimension in enumerate(["A", "C", "R"])
    ]
    first = await _page(app, kind="reviews", limit=1)
    assert first["ok"] is True, first
    backend.record["reviews"].append(
        {
            "dimension": "A",
            "verdict": "BLOCKING",
            "evidence": {"reason": "changed"},
            "warnings": [],
            "reviewed_at": "2026-10-10T20:00:04Z",
            "reviewer": "QA",
        }
    )
    stale = await _page(app, kind="reviews", cursor=first["task"]["next_cursor"])
    assert stale["ok"] is False and stale["code"] == "invalid_cursor"

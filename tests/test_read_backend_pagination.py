import sqlite3

import pytest

from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_access_slots_are_query_paginated_without_duplicates(tmp_path):
    store = AuthFoundationStore(tmp_path / "auth.sqlite3")
    await store.initialize()
    for index in range(25):
        await store.register_access_slot(f"la_{index:02d}", "secondary")

    expected = [item["logical_agent_id"] for item in await store.access_slots()]
    actual = []
    for offset in (0, 10, 20):
        page = await store.access_slots(limit=10, offset=offset)
        assert len(page) <= 10
        actual.extend(item["logical_agent_id"] for item in page)

    assert actual == expected
    assert len(actual) == len(set(actual)) == 25


@pytest.mark.asyncio
async def test_context_entries_are_query_paginated_in_public_grouping_order(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = ContextStore(repo.path)
    for index in range(7):
        await store.create(
            f"context-{index}",
            f"content-{index}",
            primary=index % 3 == 0,
        )

    expected = [
        item["id"] for item in await store.list(primary_first=True)
    ]
    actual = []
    for offset in (0, 3, 6):
        page = await store.list(limit=3, offset=offset, primary_first=True)
        assert len(page) <= 3
        actual.extend(item["id"] for item in page)

    assert actual == expected
    assert len(actual) == len(set(actual)) == 7


@pytest.mark.asyncio
async def test_task_store_limit_and_offset_cover_stable_dataset(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    for index in range(37):
        await store.create_task(
            "pagination",
            f"T-{index:03d}",
            f"Task {index}",
            priority=index % 4,
        )

    expected = [item["task_id"] for item in await store.list_tasks(namespace="pagination")]
    actual = []
    for offset in range(0, 37, 10):
        page = await store.list_tasks(namespace="pagination", limit=10, offset=offset)
        assert len(page) <= 10
        actual.extend(item["task_id"] for item in page)

    assert actual == expected
    assert len(actual) == len(set(actual)) == 37


@pytest.mark.asyncio
async def test_message_history_paginates_beyond_legacy_500_row_ceiling(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = PersistentAgentStore(repo.path)
    await store.create_slot(
        "la_history",
        "History",
        "1234",
        authority_node_id="secondary",
    )

    rows = [
        (
            f"secondary:msg:{index:04d}",
            "la_history",
            "sender",
            f"message-{index}",
            0,
            0,
            1,
            f"2026-10-05T00:{index // 60:02d}:{index % 60:02d}.000Z",
        )
        for index in range(525)
    ]
    with sqlite3.connect(repo.path) as db:
        db.executemany(
            "INSERT INTO persistent_message_obligations("
            "message_ref,logical_agent_id,sender_agent_id,text,require_reply,alert,"
            "gate_revision,created_at) VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        db.commit()

    actual = []
    for offset in range(0, 525, 100):
        page = await store.message_inbox(
            "la_history",
            show_all=True,
            limit=100,
            offset=offset,
        )
        assert len(page) <= 100
        actual.extend(item["message_ref"] for item in page)

    assert len(actual) == 525
    assert len(actual) == len(set(actual))
    assert actual[0] == "secondary:msg:0524"
    assert actual[-1] == "secondary:msg:0000"

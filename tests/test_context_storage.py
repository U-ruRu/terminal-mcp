"""Current context operations, each checked independently."""

import pytest_asyncio

from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest_asyncio.fixture
async def context(tmp_path):
    repository = SqliteRepository(tmp_path / "context.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    return ContextStore(repository.path)


async def test_context_create_returns_stored_entry(context):
    created = await context.create("Notes", "Current instructions", True)
    assert created == {
        "id": 1,
        "summary": "Notes",
        "content": "Current instructions",
        "primary": True,
    }


async def test_context_list_returns_entries_in_creation_order(context):
    first = await context.create("First", "One", False)
    second = await context.create("Second", "Two", True)
    assert await context.list() == [first, second]


async def test_context_partial_update_preserves_untouched_fields(context):
    entry = await context.create("Original", "Original content", False)
    assert await context.update(entry["id"], summary="Renamed") == {
        "id": entry["id"],
        "summary": "Renamed",
        "content": "Original content",
        "primary": False,
    }


async def test_context_delete_removes_selected_entry(context):
    selected = await context.create("Selected", "Remove", False)
    retained = await context.create("Retained", "Keep", False)
    assert await context.delete(selected["id"]) is True
    assert await context.list() == [retained]

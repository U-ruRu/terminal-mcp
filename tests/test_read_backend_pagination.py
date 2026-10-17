import pytest

from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_context_page_applies_offset_after_primary_grouping(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = ContextStore(repo.path)
    regular = await store.create("Regular", "Regular content", primary=False)
    await store.create("Primary", "Primary content", primary=True)
    await store.create("Later regular", "More content", primary=False)

    page = await store.list(limit=1, offset=1, primary_first=True)

    assert [row["id"] for row in page] == [regular["id"]]


@pytest.mark.asyncio
async def test_task_page_applies_offset_after_priority_order(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    await store.create_task("pagination", "middle", "Middle priority", priority=1)
    await store.create_task("pagination", "last", "Lowest priority", priority=0)
    await store.create_task("pagination", "first", "Highest priority", priority=2)

    page = await store.list_tasks(namespace="pagination", limit=1, offset=1)

    assert [row["task_id"] for row in page] == ["middle"]

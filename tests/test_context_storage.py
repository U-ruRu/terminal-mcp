import sqlite3

import pytest

from terminal_mcp.storage.context import ContextStore
from terminal_mcp.storage.sqlite import SqliteRepository


async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    return repo, ContextStore(repo.path)


@pytest.mark.asyncio
async def test_context_schema_and_crud_are_durable(tmp_path):
    repo, context = await store(tmp_path)
    with sqlite3.connect(repo.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        columns = {row[1] for row in db.execute("PRAGMA table_info(instance_context)")}
    assert {"id", "summary", "content", "is_primary"} <= columns

    first = await context.create("Git workflow", "Use the local Git wrapper.", True)
    second = await context.create("Docs", "Read the local documentation root.", False)
    assert first == {
        "id": 1,
        "summary": "Git workflow",
        "content": "Use the local Git wrapper.",
        "primary": True,
    }
    assert second["id"] == 2
    assert [item["id"] for item in await context.list()] == [1, 2]

    reopened = ContextStore(repo.path)
    assert await reopened.get(1) == first
    updated = await reopened.update(2, content="Updated docs.", primary=True)
    assert updated == {
        "id": 2,
        "summary": "Docs",
        "content": "Updated docs.",
        "primary": True,
    }
    assert await reopened.get(1) == first

    assert await reopened.delete(1) is True
    assert await reopened.get(1) is None
    assert await reopened.delete(1) is False
    third = await reopened.create("Services", "Use systemctl for service state.", False)
    assert third["id"] == 3
    assert [item["id"] for item in await reopened.list()] == [2, 3]


@pytest.mark.asyncio
async def test_context_validation_and_partial_update(tmp_path):
    _, context = await store(tmp_path)
    with pytest.raises(ValueError, match="summary is required"):
        await context.create("  ", "content", False)
    with pytest.raises(ValueError, match="at most 100"):
        await context.create("x" * 101, "content", False)
    with pytest.raises(ValueError, match="content is required"):
        await context.create("summary", "  ", False)
    with pytest.raises(ValueError, match="primary must be a boolean"):
        await context.create("summary", "content", 1)

    entry = await context.create("Original", "Original content", False)
    changed = await context.update(entry["id"], summary="Renamed")
    assert changed["summary"] == "Renamed"
    assert changed["content"] == "Original content"
    assert changed["primary"] is False
    with pytest.raises(ValueError, match="at least one"):
        await context.update(entry["id"])
    assert await context.update(999, content="missing") is None


@pytest.mark.asyncio
async def test_existing_v10_database_migrates_to_context_schema(tmp_path):
    database = tmp_path / "legacy-v10.sqlite3"
    repo = SqliteRepository(database, tmp_path / "output.sqlite3")
    await repo.initialize()
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE instance_context")
        db.execute("PRAGMA user_version=10")
        db.commit()

    reopened = SqliteRepository(database, tmp_path / "output.sqlite3")
    await reopened.initialize()
    context = ContextStore(reopened.path)
    created = await context.create("Migrated", "Context survives v10 migration.", True)
    assert created["id"] == 1
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 15
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

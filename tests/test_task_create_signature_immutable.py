"""Task create without task_id must retain its original duplicate signature."""

import pytest

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_implicit_create_cannot_be_repeated_after_renaming_existing_task(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(path)
    tasks = TaskCoordinator(store)
    original = dict(
        action="create",
        namespace="signature",
        task_id=None,
        title="Original candidate",
        description="Original details",
        isolation_hint="none",
    )
    first = await tasks.mutate("creator", **original)
    assert first["ok"], first
    identifier = first["task"]["task_id"]
    updated = await tasks.mutate(
        "creator",
        action="update",
        namespace="signature",
        task_id=identifier,
        expected_revision=1,
        title="Renamed candidate",
        description="Changed details",
    )
    assert updated["ok"], updated
    before = await store.list_tasks(namespace="signature")
    repeated = await tasks.mutate("creator", **original)
    assert repeated["ok"] is False and repeated["code"] == "duplicate_task", repeated
    assert await store.list_tasks(namespace="signature") == before

    # The updated content is not itself an immutable creation signature.
    second = await tasks.mutate(
        "creator", **{**original, "title": "Renamed candidate", "description": "Changed details"}
    )
    assert second["ok"], second
    assert second["task"]["task_id"] != identifier

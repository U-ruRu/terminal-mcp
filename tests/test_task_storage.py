import pytest

from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskRevisionConflict, TaskStore


async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    return repo, TaskStore(repo.path)


@pytest.mark.asyncio
async def test_task_optimistic_revision_update(tmp_path):
    _, tasks = await store(tmp_path)
    await tasks.create_task("ns", "T-1", "Current title")
    await tasks.update_task("ns", "T-1", expected_revision=1, title="Updated")

    with pytest.raises(TaskRevisionConflict) as exc:
        await tasks.update_task("ns", "T-1", expected_revision=1, title="Stale writer")

    assert exc.value.actual == 2
    assert (await tasks.get_task("ns", "T-1"))["title"] == "Updated"

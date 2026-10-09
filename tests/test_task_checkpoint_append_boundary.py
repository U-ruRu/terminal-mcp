"""Content update cannot overwrite checkpoint; checkpoint is append-only."""

import pytest
from pydantic import ValidationError

from terminal_mcp.application.task_requests import TaskUpdateRequest
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_update_rejects_checkpoint_and_preserves_append_only_history(tmp_path):
    path = tmp_path / "task-checkpoint.sqlite3"
    await SqliteRepository(path, tmp_path / "output.sqlite3").initialize()
    store = TaskStore(path)
    tasks = TaskCoordinator(store)
    first = await tasks.mutate(
        "author",
        action="create",
        namespace="checkpoint",
        task_id="one",
        title="Immutable checkpoints",
        isolation_hint="none",
    )
    assert first["ok"], first
    checkpoint = await tasks.mutate(
        "author",
        action="checkpoint",
        namespace="checkpoint",
        task_id="one",
        checkpoint={"phase": "one"},
    )
    assert checkpoint["ok"], checkpoint
    revision = (await store.get_task("checkpoint", "one"))["revision"]
    before = await store.list_events("checkpoint", "one")
    with pytest.raises(ValidationError):
        TaskUpdateRequest.model_validate(
            {
                "action": "update",
                "namespace": "checkpoint",
                "task_id": "one",
                "expected_revision": revision,
                "checkpoint": {"phase": "overwritten"},
            }
        )
    refused = await tasks.mutate(
        "author",
        action="update",
        namespace="checkpoint",
        task_id="one",
        expected_revision=revision,
        checkpoint={"phase": "overwritten"},
    )
    assert not refused["ok"], refused
    assert refused["code"] == "input_validation_failed", refused
    assert await store.list_events("checkpoint", "one") == before
    assert (await store.get_task("checkpoint", "one"))["checkpoint"] == {"phase": "one"}
    assert (await store.get_task("checkpoint", "one"))["revision"] == revision

    appended = await tasks.mutate(
        "author",
        action="checkpoint",
        namespace="checkpoint",
        task_id="one",
        checkpoint={"phase": "two"},
    )
    assert appended["ok"], appended
    assert (await store.get_task("checkpoint", "one"))["checkpoint"] == {"phase": "two"}
    assert len(await store.list_events("checkpoint", "one")) == len(before) + 1

"""Atomic task behavior retained from the compact contract review."""

import pytest

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_blank_archive_note_masks_fallback_note(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task("qa", "archive", "Archive example")
    await store.claim("qa", "archive", "owner", claim_intent="real intent")

    result = await core._action_archive(
        "owner", "qa", "archive", archive_note="   ", note="valid fallback"
    )
    assert result.get("ok") is False


@pytest.mark.asyncio
async def test_candidate_reference_is_frozen_after_completion(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task(
        "qa", "done", "Completed", state="done", result={"evidence": "ok"},
        candidate_ref="candidate-done",
    )
    revision = (await store.get_task("qa", "done"))["revision"]

    result = await core._action_update(
        "agent", "qa", "done", expected_revision=revision, candidate_ref="candidate-done"
    )
    assert result.get("code") == "candidate_ref_frozen"


@pytest.mark.asyncio
async def test_candidate_reference_is_frozen_after_incoming_review(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task("qa", "parent", "Parent", candidate_ref="candidate-a")
    await store.create_task("qa", "review", "Review", lane="review", candidate_ref="candidate-a")
    relation = await core._action_relate(
        "reviewer", "qa", "review", relation_kind="review_of", related_task_id="parent"
    )
    assert relation.get("ok") is True
    revision = (await store.get_task("qa", "parent"))["revision"]

    result = await core._action_update(
        "agent", "qa", "parent", expected_revision=revision, candidate_ref="candidate-a"
    )
    assert result.get("code") == "candidate_ref_frozen"

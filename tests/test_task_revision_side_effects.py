"""Record revisions fence side-stream writes without becoming stream counters."""

import sqlite3

import pytest
from pydantic import ValidationError

from terminal_mcp.application.task_requests import TaskCommentRequest
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskRevisionConflict, TaskStore


async def setup_store(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    await store.create_task("test", "one", "Source", output_refs=["test-output"])
    await store.create_task("test", "two", "Target")
    await store.update_task("test", "one", expected_revision=1, title="Revision two")
    return repo, store


def rows(path):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "work_items",
                "work_claims",
                "work_events",
                "work_relations",
                "work_reviews",
            )
        }


async def mutation(store, action, revision):
    if action == "claim":
        return await store.claim(
            "test", "one", "agent", claim_intent="test", expected_revision=revision
        )
    if action == "release":
        return await store.release_claim_mutation(
            "test", "one", "agent", reason="test", expected_revision=revision
        )
    if action == "review":
        return await store.upsert_reviews(
            "test",
            "one",
            dimensions=["A"],
            verdict="NON_BLOCKING",
            agent_id="agent",
            expected_revision=revision,
        )
    method = store.add_relation if action == "relate" else store.remove_relation
    return await method(
        "test",
        "one",
        related_namespace="test",
        related_task_id="two",
        relation_kind="related_to",
        agent_id="agent",
        expected_revision=revision,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["claim", "release", "review", "relate", "unrelate"])
async def test_side_stream_storage_rejects_stale_revision_atomically(tmp_path, action):
    repo, store = await setup_store(tmp_path)
    if action == "release":
        await store.claim("test", "one", "agent", claim_intent="setup")
    if action == "unrelate":
        await mutation(store, "relate", 2)
    before = rows(repo.path)
    with pytest.raises(TaskRevisionConflict) as exc:
        await mutation(store, action, 1)
    assert exc.value.actual == 2
    assert rows(repo.path) == before
    await mutation(store, action, 2)
    assert (await store.get_task("test", "one"))["revision"] == 2
    assert rows(repo.path) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["claim", "release", "review", "relate", "unrelate"])
async def test_coordinator_classifies_stale_revision_before_noop_or_mutation(tmp_path, action):
    repo, store = await setup_store(tmp_path)
    coordinator = TaskCoordinator(store)
    before = rows(repo.path)
    result = await coordinator.mutate(
        "agent",
        action=action,
        namespace="test",
        task_id="one",
        expected_revision=1,
        claim_intent="test",
        release_reason="test",
        dimensions=["A"],
        verdict="NON_BLOCKING",
        related_namespace="test",
        related_task_id="two",
        relation_kind="related_to",
    )
    assert result["ok"] is False
    assert result["code"] == "revision_conflict"
    assert result["details"]["current_revision"] == 2
    assert rows(repo.path) == before


def test_comments_use_append_semantics_and_reject_revision_argument():
    with pytest.raises(ValidationError) as exc:
        TaskCommentRequest.model_validate(
            {
                "action": "comment",
                "namespace": "test",
                "task_id": "one",
                "comment_text": "append",
                "expected_revision": 1,
            }
        )
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,method",
    [
        ("claim", "claim"),
        ("release", "release_claim_mutation"),
        ("review", "upsert_reviews"),
        ("relate", "add_relation"),
        ("unrelate", "remove_relation"),
    ],
)
async def test_revision_race_between_preflight_and_storage_is_fenced(
    tmp_path, monkeypatch, action, method
):
    _, store = await setup_store(tmp_path)
    coordinator = TaskCoordinator(store)
    original = getattr(store, method)
    if action == "release":
        await store.claim("test", "one", "agent", claim_intent="setup")

    async def no_owner_error(*args):
        return None

    async def claims(*args):
        return await store.claims("test", "one", active_only=True)

    async def raced(*args, **kwargs):
        assert kwargs["expected_revision"] == 2
        await store.update_task("test", "one", expected_revision=2, title="Concurrent revision")
        return await original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_owner_error", no_owner_error)
    monkeypatch.setattr(coordinator, "_live_claims", claims)
    monkeypatch.setattr(coordinator, "_cleanup_stale_claims", claims)
    monkeypatch.setattr(store, method, raced)
    result = await coordinator.mutate(
        "agent",
        action=action,
        namespace="test",
        task_id="one",
        expected_revision=2,
        claim_intent="test",
        release_reason="test",
        dimensions=["A"],
        verdict="NON_BLOCKING",
        related_namespace="test",
        related_task_id="two",
        relation_kind="related_to",
    )
    assert result["ok"] is False
    assert result["code"] == "revision_conflict"
    assert result["details"]["current_revision"] == 3
    assert (await store.get_task("test", "one"))["revision"] == 3

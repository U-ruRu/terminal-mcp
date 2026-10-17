"""Record revisions fence side-stream writes without becoming stream counters."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


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


@pytest.mark.asyncio
async def test_coordinator_classifies_stale_revision_before_action_handler():
    store = SimpleNamespace(get_task=AsyncMock(return_value={"revision": 2}))
    coordinator = TaskCoordinator(store)
    coordinator._action_claim = AsyncMock()
    # Project actual domain fields; the double does not manufacture a conflict.
    coordinator._result = AsyncMock(side_effect=lambda *args, **fields: fields)
    receipt = await coordinator.mutate(
        "agent",
        action="claim",
        namespace="test",
        task_id="one",
        expected_revision=1,
        _claim_owner=ClaimOwner.logical_agent("agent"),
    )
    assert (receipt["ok"], receipt["code"], receipt["details"]) == (
        False,
        "revision_conflict",
        {"current_revision": 2},
    )
    coordinator._action_claim.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,method",
    [
        ("claim", "claim_owner"),
        ("release", "release_owner_claim_mutation"),
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
        await store.claim_owner(
            "test", "one", ClaimOwner.logical_agent("agent"), claim_intent="setup"
        )
    if action == "unrelate":
        await store.add_relation(
            "test",
            "one",
            related_namespace="test",
            related_task_id="two",
            relation_kind="related_to",
            agent_id="agent",
        )
    committed_writer_state = None

    async def no_owner_error(*args):
        return None

    async def claims(*args):
        return await store.claims("test", "one", active_only=True)

    async def raced(*args, **kwargs):
        nonlocal committed_writer_state
        assert kwargs["expected_revision"] == 2
        await store.update_task("test", "one", expected_revision=2, title="Concurrent revision")
        committed_writer_state = rows(store.path)
        return await original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_owner_error", no_owner_error)
    monkeypatch.setattr(coordinator, "_live_claims", claims)
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
        _claim_owner=ClaimOwner.logical_agent("agent"),
    )
    assert result["ok"] is False
    assert result["code"] == "revision_conflict"
    assert result["details"]["current_revision"] == 3
    assert (await store.get_task("test", "one"))["revision"] == 3
    assert rows(store.path) == committed_writer_state

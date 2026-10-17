"""A current-revision identical update is an event-free retry."""

import pytest

from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.fixture
def task_names():
    return ("ops", "task")


@pytest.mark.asyncio
async def test_update_retry_with_current_revision_is_event_free(tmp_path, task_names):
    repo = SqliteRepository(tmp_path / "task.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    namespace, task_id = task_names
    await store.create_task(namespace, task_id, "Current title")
    before_events = await store.list_events(namespace, task_id)

    result = await TaskCoordinator(store).mutate(
        "author",
        action="update",
        namespace=namespace,
        task_id=task_id,
        title="Current title",
        expected_revision=1,
        _claim_owner=ClaimOwner.logical_agent("author"),
    )

    assert result["ok"] and result["task"]["revision"] == 1
    assert await store.list_events(namespace, task_id) == before_events


@pytest.mark.asyncio
async def test_cooperative_released_owner_retry_does_not_release_peer_claim(tmp_path):
    repo = SqliteRepository(tmp_path / "coop.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    created = await coordinator.mutate(
        "owner",
        action="create",
        namespace="coop",
        task_id="shared",
        title="Shared operation",
        cooperative=True,
        isolation_hint="none",
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert created["ok"], created
    for actor in ("owner", "peer"):
        claimed = await coordinator.mutate(
            actor,
            action="claim",
            namespace="coop",
            task_id="shared",
            claim_intent="cooperative acceptance",
            _claim_owner=ClaimOwner.logical_agent(actor),
        )
        assert claimed["ok"], claimed

    first = await coordinator.mutate(
        "owner",
        action="release",
        namespace="coop",
        task_id="shared",
        release_reason="handed off",
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert first["ok"], first
    events_before = len(await store.list_events("coop", "shared"))

    retry = await coordinator.mutate(
        "owner",
        action="release",
        namespace="coop",
        task_id="shared",
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert retry["ok"], retry
    assert len(await store.list_events("coop", "shared")) == events_before
    peer_claims = await store.active_claims("coop", "shared")
    assert len(peer_claims) == 1 and peer_claims[0]["agent_id"] == "peer"

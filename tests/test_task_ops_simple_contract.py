"""Task state is an independent atomic workflow value; content CAS is stable."""

import pytest

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.fixture
def task_names():
    return ("ops", "task")


@pytest.mark.asyncio
async def test_state_cas_is_independent_and_noop_is_event_free(tmp_path, task_names):
    repo = SqliteRepository(tmp_path / "task.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    namespace, task_id = task_names

    create = await coordinator.mutate(
        "agent1", action="create", namespace=namespace, task_id=task_id,
        title="Initial title", description="preserve me", isolation_hint="none",
    )
    assert create["ok"]
    base = (await store.get_task(namespace, task_id))["revision"]
    events = len(await store.list_events(namespace, task_id))

    for status in ("in_progress", "blocked", "done", "ready", "deferred", "ready"):
        result = await coordinator.mutate(
            "agent1", action="state", namespace=namespace, task_id=task_id,
            state=status,
        )
        assert result["ok"], result
        current = await store.get_task(namespace, task_id)
        assert current["state"] == status
        assert current["revision"] == base
        assert current["description"] == "preserve me"
        assert current["result"] is None
    changed_events = len(await store.list_events(namespace, task_id))
    assert changed_events == events + 6

    same = await coordinator.mutate(
        "agent1", action="state", namespace=namespace, task_id=task_id, state="ready"
    )
    assert same["ok"]
    assert len(await store.list_events(namespace, task_id)) == changed_events

    changed = await coordinator.mutate(
        "agent1", action="update", namespace=namespace, task_id=task_id,
        title="New title", expected_revision=base,
    )
    assert changed["ok"], changed
    assert changed["task"]["revision"] == base + 1

    stale = await coordinator.mutate(
        "agent1", action="update", namespace=namespace, task_id=task_id,
        title="Should reject", expected_revision=base,
    )
    assert not stale["ok"] and stale["code"] == "revision_conflict"
    assert stale["details"]["current_revision"] == base + 1
    assert (await store.get_task(namespace, task_id))["title"] == "New title"

    before_noop = len(await store.list_events(namespace, task_id))
    repeat = await coordinator.mutate(
        "agent1", action="update", namespace=namespace, task_id=task_id,
        title="New title", expected_revision=base + 1,
    )
    assert repeat["ok"] and repeat["task"]["revision"] == base + 1
    assert len(await store.list_events(namespace, task_id)) == before_noop


@pytest.mark.asyncio
async def test_atomic_claim_release_foreign_owner_and_duplicate_create(tmp_path):
    repo = SqliteRepository(tmp_path / "task.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    args = {"namespace": "ops", "task_id": "claim"}

    created = await coordinator.mutate(
        "agent1", action="create", **args, title="One", isolation_hint="none"
    )
    assert created["ok"]
    duplicate = await coordinator.mutate(
        "agent1", action="create", **args, title="Overwritten", isolation_hint="none"
    )
    assert not duplicate["ok"] and duplicate["code"] == "task_already_exists"
    assert (await store.get_task("ops", "claim"))["title"] == "One"

    second = await coordinator.mutate(
        "agent1", action="create", namespace="ops", task_id="other",
        title="One", isolation_hint="none"
    )
    assert second["ok"]
    before = await store.get_task("ops", "claim")
    own = await coordinator.mutate(
        "agent1", action="claim", **args, claim_intent="implement"
    )
    assert own["ok"]
    again = await coordinator.mutate(
        "agent1", action="claim", **args, claim_intent="implement"
    )
    assert again["ok"]
    assert len(await store.active_claims("ops", "claim")) == 1
    foreign = await coordinator.mutate(
        "agent2", action="claim", **args, claim_intent="competing"
    )
    assert not foreign["ok"] and foreign["code"] == "already_claimed"
    denied_state = await coordinator.mutate(
        "agent2", action="state", **args, state="done"
    )
    assert not denied_state["ok"] and denied_state["code"] == "owner_required"
    denied_release = await coordinator.mutate("agent2", action="release", **args)
    assert not denied_release["ok"] and denied_release["code"] == "not_owner"

    released = await coordinator.mutate("agent1", action="release", **args)
    assert released["ok"]
    repeated = await coordinator.mutate("agent1", action="release", **args)
    assert repeated["ok"]
    assert not await store.active_claims("ops", "claim")
    after = await store.get_task("ops", "claim")
    assert after["revision"] == before["revision"]
    assert after["state"] == before["state"]


@pytest.mark.asyncio
async def test_review_state_fans_out_feedback_atomically_without_content_revision(tmp_path):
    repo = SqliteRepository(tmp_path / "reviews.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    ns = "ops-review"
    for task_id, lane in (("implementation", "implementation"), ("review", "review")):
        created = await coordinator.mutate(
            "reviewer", action="create", namespace=ns, task_id=task_id,
            title=task_id, lane=lane, isolation_hint="none",
            candidate_ref="candidate-1",
        )
        assert created["ok"], created
    related = await coordinator.mutate(
        "reviewer", action="relate", namespace=ns, task_id="review",
        relation_kind="review_of", related_namespace=ns,
        related_task_id="implementation",
    )
    assert related["ok"], related
    revision = (await store.get_task(ns, "review"))["revision"]

    blocked = await coordinator.mutate(
        "reviewer", action="state", namespace=ns, task_id="review",
        state="blocked", blocker_reason="A specific blocking finding.",
    )
    assert blocked["ok"], blocked
    feedback = [
        event for event in await store.list_events(ns, "implementation")
        if event["event_type"] == "review_feedback"
    ]
    assert len(feedback) == 1
    assert feedback[0]["payload"]["outcome"] == "blocked"
    assert feedback[0]["payload"]["blocker_reason"] == "A specific blocking finding."
    assert feedback[0]["payload"]["candidate_ref"] == "candidate-1"

    repeat = await coordinator.mutate(
        "reviewer", action="state", namespace=ns, task_id="review",
        state="blocked", blocker_reason="Same finding.",
    )
    assert repeat["ok"]
    assert len([
        event for event in await store.list_events(ns, "implementation")
        if event["event_type"] == "review_feedback"
    ]) == 1
    after = await store.get_task(ns, "review")
    assert after["revision"] == revision
    assert after["result"] is None


@pytest.mark.asyncio
async def test_cooperative_released_owner_retry_does_not_release_peer_claim(tmp_path):
    repo = SqliteRepository(tmp_path / "coop.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    created = await coordinator.mutate(
        "owner", action="create", namespace="coop", task_id="shared",
        title="Shared operation", cooperative=True, isolation_hint="none",
    )
    assert created["ok"], created
    for actor in ("owner", "peer"):
        claimed = await coordinator.mutate(
            actor, action="claim", namespace="coop", task_id="shared",
            claim_intent="cooperative acceptance",
        )
        assert claimed["ok"], claimed

    first = await coordinator.mutate(
        "owner", action="release", namespace="coop", task_id="shared",
        release_reason="handed off",
    )
    assert first["ok"], first
    events_before = len(await store.list_events("coop", "shared"))

    retry = await coordinator.mutate(
        "owner", action="release", namespace="coop", task_id="shared"
    )
    assert retry["ok"], retry
    assert len(await store.list_events("coop", "shared")) == events_before
    peer_claims = await store.active_claims("coop", "shared")
    assert len(peer_claims) == 1 and peer_claims[0]["agent_id"] == "peer"

    foreign = await coordinator.mutate(
        "intruder", action="release", namespace="coop", task_id="shared"
    )
    assert foreign["ok"] is False and foreign["code"] == "not_owner"
    assert len(await store.active_claims("coop", "shared")) == 1

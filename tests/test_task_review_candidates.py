"""Current review relations and workflow feedback, owned by TaskStore."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskRelationConflict, TaskStore


@pytest_asyncio.fixture
async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "reviews.sqlite3")
    await repo.initialize()
    return TaskStore(repo.path)


async def review_pair(store, *, parent_candidate="candidate-a", review_candidate=None):
    await store.create_task("review", "parent", "Implementation", candidate_ref=parent_candidate)
    await store.create_task(
        "review", "check", "Review", lane="review", candidate_ref=review_candidate
    )


async def link(store, parent="parent"):
    return await store.add_relation(
        "review",
        "check",
        related_namespace="review",
        related_task_id=parent,
        relation_kind="review_of",
        agent_id="reviewer",
        capture_task=True,
    )


@pytest.mark.asyncio
async def test_review_relation_binds_the_parent_candidate_in_its_commit(store):
    await review_pair(store)

    committed = await link(store)

    assert (committed["candidate_ref"], committed["revision"]) == ("candidate-a", 2)
    events = await store.list_events("review", "check")
    assert [event["event_type"] for event in events] == ["relation_added", "candidate_bound"]
    assert events[1]["payload"] == {
        "candidate_ref": "candidate-a",
        "parent_namespace": "review",
        "parent_task_id": "parent",
    }


@pytest.mark.asyncio
async def test_review_relation_requires_a_parent_candidate(store):
    await review_pair(store, parent_candidate=None)

    with pytest.raises(TaskRelationConflict) as error:
        await link(store)

    assert error.value.code == "missing_parent_candidate"
    assert await store.relations("review", "check") == []


@pytest.mark.asyncio
async def test_review_relation_rejects_a_different_candidate(store):
    await review_pair(store, review_candidate="candidate-other")

    with pytest.raises(TaskRelationConflict) as error:
        await link(store)

    assert error.value.code == "candidate_conflict"
    assert (await store.get_task("review", "check"))["candidate_ref"] == "candidate-other"


@pytest.mark.asyncio
async def test_review_relation_has_one_parent(store):
    await review_pair(store)
    await link(store)
    await store.create_task("review", "other", "Other parent", candidate_ref="candidate-a")

    with pytest.raises(TaskRelationConflict) as error:
        await link(store, "other")

    assert error.value.code == "ambiguous_review_parent"
    assert [row["task_id"] for row in await store.relations("review", "check")] == ["parent"]


@pytest.mark.asyncio
async def test_review_relation_requires_the_review_lane(store):
    await store.create_task("review", "parent", "Parent", candidate_ref="candidate-a")
    await store.create_task("review", "check", "Implementation", lane="implementation")

    with pytest.raises(TaskRelationConflict) as error:
        await link(store)

    assert error.value.code == "review_task_required"


@pytest.mark.asyncio
async def test_candidate_update_is_allowed_before_review(store):
    await store.create_task("review", "parent", "Parent", candidate_ref="candidate-a")

    result = await TaskCoordinator(store).mutate(
        "author",
        action="update",
        namespace="review",
        task_id="parent",
        candidate_ref="candidate-b",
        expected_revision=1,
    )

    assert result["ok"] is True
    assert (await store.get_task("review", "parent"))["candidate_ref"] == "candidate-b"


@pytest.mark.asyncio
async def test_removing_a_relation_preserves_its_audit_history(store):
    await review_pair(store)
    await store.add_relation(
        "review",
        "check",
        related_namespace="review",
        related_task_id="parent",
        relation_kind="related",
        agent_id="author",
    )

    await store.remove_relation(
        "review",
        "check",
        related_namespace="review",
        related_task_id="parent",
        relation_kind="related",
        agent_id="author",
    )

    assert await store.relations("review", "check") == []
    assert [event["event_type"] for event in await store.list_events("review", "check")] == [
        "relation_removed",
        "relation_added",
    ]


@pytest.mark.asyncio
async def test_completed_review_state_publishes_feedback_for_the_bound_candidate(store):
    await review_pair(store)
    await link(store)

    await store.set_workflow_state(
        "review", "check", "done", agent_id="reviewer", result={"evidence": "checked"}
    )

    events = await store.list_events("review", "parent")
    assert [(event["event_type"], event["payload"]) for event in events] == [
        (
            "review_feedback",
            {
                "review_namespace": "review",
                "review_task_id": "check",
                "outcome": "done",
                "candidate_ref": "candidate-a",
                "result": {"evidence": "checked"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_review_feedback_failure_rolls_back_the_state_change(store):
    await review_pair(store)
    await link(store)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_feedback BEFORE INSERT ON work_events "
            "WHEN NEW.event_type='review_feedback' "
            "BEGIN SELECT RAISE(ABORT,'feedback failed'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="feedback failed"):
        await store.set_workflow_state("review", "check", "done", agent_id="reviewer")

    assert (await store.get_task("review", "check"))["state"] == "ready"

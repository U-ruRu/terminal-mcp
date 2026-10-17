"""Force ownership/revision changes at the boundary between preflight and commit."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def case(tmp_path):
    repo = SqliteRepository(tmp_path / "tasks.db", tmp_path / "output.db")
    await repo.initialize()
    store = TaskStore(repo.path)
    await store.create_task(
        "atomic",
        "task",
        "Original",
        state="in_progress",
        checkpoint={"keep": 1},
        result={"evidence": "keep"},
    )
    await store.create_task("atomic", "other", "Related task")
    await store.claim_owner(
        "atomic", "task", ClaimOwner.logical_agent("owner"), claim_intent="original"
    )
    return store, TaskCoordinator(store)


def audit(store):
    with sqlite3.connect(store.path) as db:
        return db.execute(
            "SELECT event_type,agent_id,payload_json FROM work_events "
            "WHERE namespace='atomic' AND task_id='task' AND event_type!='claim' ORDER BY id"
        ).fetchall()


OWNED_MUTATIONS = [
    # Property writes and the two relation stores have distinct transaction fences.
    ("update", {"priority": "P0"}, "update_task_mutation"),
    ("relate", {"related_task_id": "other", "relation_kind": "related"}, "add_relation"),
    ("unrelate", {"related_task_id": "other", "relation_kind": "related"}, "remove_relation"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,fields,method", OWNED_MUTATIONS)
async def test_owner_swap_is_fenced_without_relying_on_task_revision(
    case, monkeypatch, action, fields, method
):
    store, coordinator = case
    original = getattr(store, method)
    before = await store.get_task("atomic", "task")
    before_events = audit(store)
    old_claim = (await store.active_claims("atomic", "task"))[0]

    async def successor(*args, **kwargs):
        assert kwargs["expected_claim_ids"] == (old_claim["id"],)
        # Claim handoff intentionally does not increment content revision.
        await store.release_owner_claims(
            namespace="atomic", task_id="task", owner=ClaimOwner.logical_agent("owner")
        )
        await store.claim_owner(
            "atomic", "task", ClaimOwner.logical_agent("successor"), claim_intent="handoff"
        )
        assert (await store.get_task("atomic", "task"))["revision"] == before["revision"]
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, method, successor)
    result = await coordinator.mutate(
        "owner",
        action=action,
        namespace="atomic",
        task_id="task",
        **fields,
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert result["ok"] is False and result["code"] == "owner_required", result
    assert result["outcome"] == "not_committed"
    assert await store.get_task("atomic", "task") == before
    assert audit(store) == before_events
    assert [row["agent_id"] for row in await store.active_claims("atomic", "task")] == ["successor"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields",
    [
        ("checkpoint", {"checkpoint": {"after_handoff": True}}),
        ("state", {"state": "deferred"}),
    ],
)
async def test_state_and_checkpoint_remain_allowed_after_claim_handoff(case, action, fields):
    store, coordinator = case
    before = await store.get_task("atomic", "task")
    await store.release_owner_claims(
        namespace="atomic", task_id="task", owner=ClaimOwner.logical_agent("owner")
    )
    await store.claim_owner(
        "atomic", "task", ClaimOwner.logical_agent("successor"), claim_intent="handoff"
    )
    result = await coordinator.mutate(
        "owner",
        action=action,
        namespace="atomic",
        task_id="task",
        **fields,
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert result["ok"], result
    current = await store.get_task("atomic", "task")
    assert current["revision"] == before["revision"]
    assert [c["agent_id"] for c in await store.active_claims("atomic", "task")] == ["successor"]
    if action == "checkpoint":
        assert current["checkpoint"] == fields["checkpoint"]
        assert current["state"] == before["state"]
    else:
        assert current["state"] == fields["state"]
        assert current["checkpoint"] == before["checkpoint"]


@pytest.mark.asyncio
async def test_safe_participant_property_update_does_not_require_exclusive_owner(case, monkeypatch):
    store, coordinator = case
    original = store.update_task_mutation

    async def handoff(*args, **kwargs):
        assert kwargs["expected_claim_ids"] is None
        await store.release_owner_claims(
            namespace="atomic", task_id="task", owner=ClaimOwner.logical_agent("owner")
        )
        await store.claim_owner(
            "atomic", "task", ClaimOwner.logical_agent("successor"), claim_intent="handoff"
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "update_task_mutation", handoff)
    result = await coordinator.mutate(
        "owner",
        action="update",
        namespace="atomic",
        task_id="task",
        title="Clarified",
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert result["ok"], result
    assert result["task"]["title"] == "Clarified"
    assert result["task"]["owner"]["agent_name"] == "successor"


@pytest.mark.asyncio
async def test_claim_policy_race_uses_implicit_revision_guard(case, monkeypatch):
    store, coordinator = case
    await store.update_task("atomic", "task", cooperative=True)
    original = store.claim_owner

    async def policy_changed(*args, **kwargs):
        assert kwargs["expected_revision"] is not None
        await store.update_task("atomic", "task", cooperative=False)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "claim_owner", policy_changed)
    result = await coordinator.mutate(
        "other",
        action="claim",
        namespace="atomic",
        task_id="task",
        claim_intent="participant",
        _claim_owner=ClaimOwner.logical_agent("other"),
    )
    assert result["ok"] is False and result["code"] == "revision_conflict", result
    assert [row["agent_id"] for row in await store.active_claims("atomic", "task")] == ["owner"]
    assert not audit(store)


@pytest.mark.asyncio
async def test_review_audit_failure_rolls_back_review_and_revision(case):
    store, coordinator = case
    await store.update_task("atomic", "task", output_refs=["artifact:review-candidate"])
    before = await store.get_task("atomic", "task")
    before_events = audit(store)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_review_audit BEFORE INSERT ON work_events "
            "WHEN NEW.event_type='review' BEGIN SELECT RAISE(ABORT,'audit failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        await coordinator.mutate(
            "reviewer",
            action="review",
            namespace="atomic",
            task_id="task",
            dimensions=["A"],
            verdict="NON_BLOCKING",
            evidence={"proof": "checked"},
            _claim_owner=ClaimOwner.logical_agent("reviewer"),
        )
    assert await store.get_task("atomic", "task") == before
    assert not await store.reviews("atomic", "task")
    assert audit(store) == before_events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields,method",
    [
        ("claim", {"claim_intent": "first claim"}, "claim_owner"),
        ("release", {"release_reason": "handoff"}, "release_owner_claim_mutation"),
        ("comment", {"comment_text": "Committed comment"}, "append_comment"),
        ("relate", {"related_task_id": "other", "relation_kind": "related"}, "add_relation"),
        ("unrelate", {"related_task_id": "other", "relation_kind": "related"}, "remove_relation"),
        ("review", {"dimensions": ["A"], "verdict": "NON_BLOCKING"}, "upsert_reviews"),
    ],
)
async def test_side_stream_receipt_survives_all_postcommit_projection_failures(
    case, monkeypatch, action, fields, method
):
    store, coordinator = case
    if action == "claim":
        await store.release_owner_claims(
            namespace="atomic", task_id="task", owner=ClaimOwner.logical_agent("owner")
        )
    if action == "review":
        await store.update_task("atomic", "task", output_refs=["artifact:review-candidate"])
    original = getattr(store, method)
    committed = None
    failure_installed = False

    async def unavailable(*args, **kwargs):
        raise AssertionError("post-commit database/projection read")

    async def committed_then_unavailable(*args, **kwargs):
        nonlocal committed, failure_installed
        if action != "comment":
            assert kwargs["capture_task"] is True
        committed = await original(*args, **kwargs)
        monkeypatch.setattr(store, "get_task", unavailable)
        monkeypatch.setattr(coordinator, "_decorate", unavailable)
        failure_installed = True
        return committed

    monkeypatch.setattr(store, method, committed_then_unavailable)
    result = await coordinator.mutate(
        "owner",
        action=action,
        namespace="atomic",
        task_id="task",
        **fields,
        _claim_owner=ClaimOwner.logical_agent("owner"),
    )
    assert failure_installed
    assert result["ok"], result
    assert result["task"]["revision"] == committed["revision"]


@pytest.mark.asyncio
async def test_review_never_commits_against_newer_output_candidate(case, monkeypatch):
    store, coordinator = case
    await store.update_task("atomic", "task", output_refs=["artifact:original"])
    original = store.upsert_reviews

    async def new_output(*args, **kwargs):
        assert kwargs["expected_revision"] is not None
        await store.update_task("atomic", "task", output_refs=["artifact:successor"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "upsert_reviews", new_output)
    result = await coordinator.mutate(
        "reviewer",
        action="review",
        namespace="atomic",
        task_id="task",
        dimensions=["A"],
        verdict="NON_BLOCKING",
        _claim_owner=ClaimOwner.logical_agent("reviewer"),
    )
    assert result["ok"] is False
    assert result["code"] in {"revision_conflict", "output_state_changed"}
    assert not await store.reviews("atomic", "task")
    assert not any(event[0] == "review" for event in audit(store))

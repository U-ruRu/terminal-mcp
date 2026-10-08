"""Force ownership/revision changes at the boundary between preflight and commit."""

import sqlite3

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.agents import AgentStore
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
    await store.claim("atomic", "task", "owner", claim_intent="original")
    return store, TaskCoordinator(store)


def audit(store):
    with sqlite3.connect(store.path) as db:
        return db.execute(
            "SELECT event_type,agent_id,payload_json FROM work_events "
            "WHERE namespace='atomic' AND task_id='task' AND event_type!='claim' ORDER BY id"
        ).fetchall()


OWNED_MUTATIONS = [
    ("checkpoint", {"checkpoint": {"overwrite": True}}, "update_task_mutation"),
    ("state", {"state": "deferred"}, "update_task_mutation"),
    ("done", {"result": {"evidence": "finished"}}, "update_task_mutation"),
    ("archive", {"archive_note": "superseded"}, "update_task_mutation"),
    ("update", {"priority": "P0"}, "update_task_mutation"),
    ("update", {"dependencies": []}, "update_task_mutation"),
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
        # Legacy/durable claim changes intentionally do not increment task revision.
        await store.release_claims(namespace="atomic", task_id="task", agent_id="owner")
        await store.claim("atomic", "task", "successor", claim_intent="handoff")
        assert (await store.get_task("atomic", "task"))["revision"] == before["revision"]
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, method, successor)
    result = await coordinator.mutate(
        "owner", action=action, namespace="atomic", task_id="task", **fields
    )
    assert result["ok"] is False and result["code"] == "owner_required", result
    assert result["outcome"] == "not_committed"
    assert await store.get_task("atomic", "task") == before
    assert audit(store) == before_events
    assert [row["agent_id"] for row in await store.active_claims("atomic", "task")] == ["successor"]


@pytest.mark.asyncio
async def test_safe_participant_property_update_does_not_require_exclusive_owner(case, monkeypatch):
    store, coordinator = case
    original = store.update_task_mutation

    async def handoff(*args, **kwargs):
        assert kwargs["expected_claim_ids"] is None
        await store.release_claims(namespace="atomic", task_id="task", agent_id="owner")
        await store.claim("atomic", "task", "successor", claim_intent="handoff")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "update_task_mutation", handoff)
    result = await coordinator.mutate(
        "owner", action="update", namespace="atomic", task_id="task", title="Clarified"
    )
    assert result["ok"], result
    assert result["task"]["title"] == "Clarified"
    assert result["task"]["state"] == "in_progress"
    assert result["task"]["owner"]["agent_name"] == "successor"


@pytest.mark.asyncio
async def test_delayed_release_cannot_release_new_claim_by_same_owner(case, monkeypatch):
    store, coordinator = case
    original = store.release_claim_mutation
    old = (await store.active_claims("atomic", "task"))[0]
    new = None

    async def reacquire(*args, **kwargs):
        nonlocal new
        assert kwargs["expected_claim_id"] == old["id"]
        await store.release_claims(namespace="atomic", task_id="task", agent_id="owner")
        new = await store.claim("atomic", "task", "owner", claim_intent="new cycle")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "release_claim_mutation", reacquire)
    result = await coordinator.mutate(
        "owner", action="release", namespace="atomic", task_id="task", release_reason="late"
    )
    assert result["ok"], result
    assert [row["id"] for row in await store.active_claims("atomic", "task")] == [new["id"]]
    assert new["id"] != old["id"]
    assert result["task"]["owner"]["agent_name"] == "owner"
    assert not audit(store)


@pytest.mark.asyncio
async def test_claim_policy_race_uses_implicit_revision_guard(case, monkeypatch):
    store, coordinator = case
    await store.update_task("atomic", "task", cooperative=True)
    original = store.claim

    async def policy_changed(*args, **kwargs):
        assert kwargs["expected_revision"] is not None
        await store.update_task("atomic", "task", cooperative=False)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "claim", policy_changed)
    result = await coordinator.mutate(
        "other", action="claim", namespace="atomic", task_id="task", claim_intent="participant"
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
        )
    assert await store.get_task("atomic", "task") == before
    assert not await store.reviews("atomic", "task")
    assert audit(store) == before_events


@pytest.mark.asyncio
async def test_comment_append_remains_valid_during_unrelated_revision_change(case, monkeypatch):
    store, coordinator = case
    original = store.add_event

    async def other_writer(*args, **kwargs):
        assert kwargs.get("expected_revision") is None
        await store.update_task("atomic", "task", title="Concurrent title")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "add_event", other_writer)
    result = await coordinator.mutate(
        "reviewer",
        action="comment",
        namespace="atomic",
        task_id="task",
        comment_text="Independent observation",
    )
    assert result["ok"], result
    assert result["task"]["title"] == "Concurrent title"
    assert len(audit(store)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fields,method",
    [
        ("claim", {"claim_intent": "first claim"}, "claim"),
        ("release", {"release_reason": "handoff"}, "release_claim_mutation"),
        ("comment", {"comment_text": "Committed comment"}, "add_event"),
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
        await store.release_claims(namespace="atomic", task_id="task", agent_id="owner")
    if action == "review":
        await store.update_task("atomic", "task", output_refs=["artifact:review-candidate"])
    original = getattr(store, method)
    committed = None

    async def unavailable(*args, **kwargs):
        raise AssertionError("post-commit database/projection read")

    async def committed_then_unavailable(*args, **kwargs):
        nonlocal committed
        assert kwargs["capture_task"] is True
        committed = await original(*args, **kwargs)
        monkeypatch.setattr(store, "get_task", unavailable)
        monkeypatch.setattr(coordinator, "_decorate", unavailable)
        return committed

    monkeypatch.setattr(store, method, committed_then_unavailable)
    result = await coordinator.mutate(
        "owner", action=action, namespace="atomic", task_id="task", **fields
    )
    assert result["ok"], result
    assert result["task"]["revision"] == committed["revision"]
    assert result["task"]["state"] == committed["state"] == "in_progress"
    assert result["task"]["checkpoint"] == {"keep": 1}
    assert result["task"]["result"] == {"evidence": "keep"}


@pytest.mark.asyncio
async def test_committed_receipt_captures_legacy_liveness_before_subsequent_session_change(
    case, monkeypatch
):
    store, _ = case
    coordinator = TaskCoordinator(store, agent_store=AgentStore(store.path))
    stamp = utc_text()
    with sqlite3.connect(store.path) as db:
        for agent_id, state in (("owner", "ended"), ("reviewer", "active")):
            db.execute(
                "INSERT INTO agent_sessions(agent_id,registered_at,last_activity_at,"
                "task_summary,intent,work_scope,state) VALUES(?,?,?,?,?,?,?)",
                (agent_id, stamp, stamp, "test", "test", "[]", state),
            )
    original = store.add_event

    async def newer_session_state(*args, **kwargs):
        committed = await original(*args, **kwargs)
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE agent_sessions SET state='active' WHERE agent_id='owner'")
        return committed

    monkeypatch.setattr(store, "add_event", newer_session_state)
    result = await coordinator.mutate(
        "reviewer",
        action="comment",
        namespace="atomic",
        task_id="task",
        comment_text="Snapshot ownership",
    )
    assert result["ok"] and result["task"]["owner"] is None
    assert result["task"]["state"] == "in_progress"
    current = await coordinator._decorate(await store.get_task("atomic", "task"), details=False)
    assert current["owner"]["agent_name"] == "owner"


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
    )
    assert result["ok"] is False
    assert result["code"] in {"revision_conflict", "output_state_changed"}
    assert not await store.reviews("atomic", "task")
    assert not any(event[0] == "review" for event in audit(store))

import sqlite3

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskRevisionConflict, TaskStore


async def store(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    return repo, TaskStore(repo.path)


@pytest.mark.asyncio
async def test_task_schema_create_list_and_json_round_trip(tmp_path):
    repo, tasks = await store(tmp_path)
    with sqlite3.connect(repo.path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == 7
    assert {
        "work_items",
        "work_claims",
        "work_dependencies",
        "work_reviews",
        "work_events",
    } <= tables

    created = await tasks.create_task(
        "project",
        "REV-1",
        "Implement storage",
        lane="implementation",
        priority=50,
        description="Durable task registry",
        next_action="Write tests",
        resource={"repo": "/srv/repo"},
        reviews=["A", "C"],
        cooperative=True,
        checkpoint={"done": ["schema"]},
        candidate_ref="abc123",
    )
    assert created["namespace"] == "project"
    assert created["task_id"] == "REV-1"
    assert created["revision"] == 1
    assert created["resource"] == {"repo": "/srv/repo"}
    assert created["reviews"] == ["A", "C"]
    assert created["cooperative"] is True
    assert created["checkpoint"] == {"done": ["schema"]}
    assert created["candidate_ref"] == "abc123"

    await tasks.create_task("project", "DONE-1", "Old", state="done")
    assert [item["task_id"] for item in await tasks.list_tasks(namespace="project")] == ["REV-1"]
    assert {
        item["task_id"] for item in await tasks.list_tasks(namespace="project", show_done=True)
    } == {
        "REV-1",
        "DONE-1",
    }

    with pytest.raises(ValueError):
        await tasks.create_task("project", "BAD-LANE", "Bad", lane="other")
    with pytest.raises(ValueError):
        await tasks.create_task("project", "BAD-STATE", "Bad", state="active")


@pytest.mark.asyncio
async def test_task_optimistic_revision_update(tmp_path):
    _, tasks = await store(tmp_path)
    task = await tasks.create_task("ns", "T-1", "One")
    updated = await tasks.update_task(
        "ns",
        "T-1",
        expected_revision=task["revision"],
        state="blocked",
        checkpoint={"reason": "dependency"},
        candidate_ref="deadbeef",
    )
    assert updated["revision"] == 2
    assert updated["state"] == "blocked"
    assert updated["checkpoint"] == {"reason": "dependency"}
    assert updated["candidate_ref"] == "deadbeef"
    with pytest.raises(TaskRevisionConflict) as exc:
        await tasks.update_task("ns", "T-1", expected_revision=1, title="stale")
    assert exc.value.actual == 2
    assert (await tasks.get_task("ns", "T-1"))["title"] == "One"


@pytest.mark.asyncio
async def test_multi_claim_and_release(tmp_path):
    _, tasks = await store(tmp_path)
    await tasks.create_task("ns", "T-1", "One", cooperative=True)
    first = await tasks.claim("ns", "T-1", "Alpha-1111")
    duplicate = await tasks.claim("ns", "T-1", "Alpha-1111")
    second = await tasks.claim("ns", "T-1", "Bravo-2222")
    assert first["created"] is True
    assert duplicate["created"] is False
    assert second["created"] is True
    assert [item["agent_id"] for item in await tasks.active_claims("ns", "T-1")] == [
        "Alpha-1111",
        "Bravo-2222",
    ]
    assert await tasks.release_claim("ns", "T-1", "Alpha-1111") is True
    assert [item["agent_id"] for item in await tasks.active_claims("ns", "T-1")] == ["Bravo-2222"]
    assert await tasks.release_claims(agent_id="Bravo-2222") == 1
    assert await tasks.active_claims("ns", "T-1") == []


@pytest.mark.asyncio
async def test_dependencies_reviews_and_event_cursor(tmp_path):
    _, tasks = await store(tmp_path)
    await tasks.create_task("ns", "T-1", "One", candidate_ref="sha1")
    deps = await tasks.set_dependencies(
        "ns", "T-1", ["T-0", {"namespace": "other", "task_id": "EXT-2"}]
    )
    assert [(item["namespace"], item["task_id"]) for item in deps] == [
        ("ns", "T-0"),
        ("other", "EXT-2"),
    ]

    review = await tasks.upsert_review(
        "ns",
        "T-1",
        candidate_ref="sha1",
        dimension="C",
        verdict="approved",
        agent_id="Reviewer-1",
        evidence={"tests": 12},
        warnings=[{"code": "self_review"}],
    )
    assert review["dimension"] == "C"
    assert review["evidence"] == {"tests": 12}
    assert review["warnings"] == [{"code": "self_review"}]
    review2 = await tasks.upsert_review(
        "ns",
        "T-1",
        candidate_ref="sha1",
        dimension="C",
        verdict="blocking",
        agent_id="Reviewer-2",
    )
    assert review2["verdict"] == "blocking"
    assert len(await tasks.reviews("ns", "T-1", candidate_ref="sha1")) == 1
    with pytest.raises(ValueError):
        await tasks.upsert_review(
            "ns", "T-1", candidate_ref="sha1", dimension="X", verdict="ok", agent_id="R"
        )

    first = await tasks.add_event("ns", "T-1", "created", payload={"a": 1})
    second = await tasks.add_event("ns", "T-1", "checkpoint", agent_id="A", payload={"b": 2})
    recent = await tasks.list_events("ns", "T-1", limit=1)
    assert recent[0]["id"] == second["id"]
    older = await tasks.list_events("ns", "T-1", before_id=second["id"])
    assert [item["id"] for item in older] == [first["id"]]

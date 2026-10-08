"""Independent reviewer: cleanup must preserve manually established in_progress state."""

from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore


@pytest.mark.asyncio
async def test_lease_release_preserves_prior_explicit_in_progress(tmp_path):
    repo = SqliteRepository(tmp_path / "review.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = WorkWindowStore(repo.path, authority_node_id="home")
    now = utc_now()
    await store.create_slot(
        "la_one",
        "Agent One",
        "ABCD",
        authority_node_id="home",
        initial_arm_duration_seconds=1380,
        now=utc_text(now),
    )
    session = await store.start_managed_session(
        "la_one",
        role="executor",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=now,
    )
    tasks = TaskStore(repo.path)
    await tasks.create_task("review", "x", "manual active task", checkpoint={"manual": "yes"})
    await tasks.update_task("review", "x", state="in_progress")
    assert (await tasks.get_task("review", "x"))["state"] == "in_progress"
    lease = {
        "work_session_id": session.session.work_session_id,
        "session_epoch": session.session.session_epoch,
        "hard_expires_at": session.session.hard_expires_at,
    }
    await tasks.claim_owner("review", "x", ClaimOwner.logical_agent("la_one"), lease=lease)
    assert (await tasks.get_task("review", "x"))["state"] == "in_progress"
    sid = session.session.work_session_id
    epoch = session.session.session_epoch
    at = now + timedelta(seconds=1)
    await store.begin_managed_stop(
        "la_one", sid, epoch, principal_id="principal", reason="session_end", now=at
    )
    await store.finish_managed_stop("la_one", sid, epoch, principal_id="principal", now=at)
    task = await tasks.get_task("review", "x")
    assert await tasks.active_claims("review", "x") == []
    assert task["state"] == "in_progress", task
    assert task["checkpoint"] == {"manual": "yes"}

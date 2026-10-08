"""Idempotent re-claim under a temporary WorkSession must preserve explicit durable ownership."""

from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore


@pytest.mark.asyncio
async def test_durable_claim_not_silently_downgraded_by_session_reclaim(tmp_path):
    repo = SqliteRepository(tmp_path / "review.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    store = WorkWindowStore(repo.path, authority_node_id="home")
    now = utc_now()
    await store.create_slot(
        "la_one",
        "One",
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
    await tasks.create_task("review", "x", "explicit operator durable claim")
    owner = ClaimOwner.logical_agent("la_one")
    durable = await tasks.claim_owner(
        "review", "x", owner, claim_intent="persistent responsibility"
    )
    assert durable["created"]
    lease = {
        "work_session_id": session.session.work_session_id,
        "session_epoch": session.session.session_epoch,
        "hard_expires_at": session.session.hard_expires_at,
    }
    updated = await tasks.claim_owner(
        "review", "x", owner, claim_intent="temporary observation", lease=lease
    )
    assert updated["created"] is False
    sid, epoch = session.session.work_session_id, session.session.session_epoch
    at = now + timedelta(seconds=1)
    await store.begin_managed_stop(
        "la_one", sid, epoch, principal_id="principal", reason="session_end", now=at
    )
    await store.finish_managed_stop("la_one", sid, epoch, principal_id="principal", now=at)
    active = await tasks.active_claims("review", "x")
    assert len(active) == 1, "durable claim must survive session end"
    assert active[0]["owner_id"] == "la_one"

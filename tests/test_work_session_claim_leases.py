from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from native_runtime_helpers import native_agent

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.persistent_agents import PersistentAgentStore, PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest_asyncio.fixture
async def lease_case(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    sessions = PersistentAgentStore(repo.path)
    now = utc_now()
    _, identity = native_agent(repo.path, "la_one", now=now, session_id="lease-session")
    native_agent(repo.path, "la_two", code="5678", now=now)
    session = SimpleNamespace(session=await sessions.get_work_session(identity["work_session_id"]))
    tasks = TaskStore(repo.path)
    await tasks.create_task("lease-test", "task", "claim lease", checkpoint={"keep": "checkpoint"})
    return sessions, tasks, session, now


def lease(snapshot):
    return dict(
        work_session_id=snapshot.session.work_session_id,
        session_epoch=snapshot.session.session_epoch,
        hard_expires_at=snapshot.session.hard_expires_at,
    )


async def stop(sessions, snapshot, now, reason="session_end"):
    args = ("la_one", snapshot.session.work_session_id, snapshot.session.session_epoch)
    tasks = TaskStore(sessions.path)
    async with sessions._connect() as db:
        await db.execute(
            "UPDATE logical_agent_work_sessions SET state='ended' WHERE work_session_id=?",
            (snapshot.session.work_session_id,),
        )
        await db.commit()
    await tasks.fence_claim_session(*args, reason=reason, now=utc_text(now))
    return await tasks.release_work_session_claims(*args, reason=reason, now=utc_text(now))


@pytest.mark.asyncio
async def test_session_end_releases_leased_claim_preserving_task_checkpoint(lease_case):
    sessions, tasks, first, now = lease_case
    await tasks.update_task("lease-test", "task", state="in_progress")
    await tasks.claim_owner(
        "lease-test", "task", ClaimOwner.logical_agent("la_one"), lease=lease(first)
    )
    await stop(sessions, first, now + timedelta(seconds=2))
    assert await tasks.active_claims("lease-test", "task") == []
    task = await tasks.get_task("lease-test", "task")
    assert task["state"] == "in_progress"
    assert task["checkpoint"] == {"keep": "checkpoint"}


@pytest.mark.asyncio
async def test_explicit_durable_ownership_survives_session_end(lease_case):
    sessions, tasks, first, now = lease_case
    await tasks.update_task("lease-test", "task", state="in_progress")
    await tasks.claim_owner("lease-test", "task", ClaimOwner.logical_agent("la_one"))
    claims = await tasks.active_claims("lease-test", "task")
    await stop(sessions, first, now + timedelta(seconds=1))
    assert await tasks.active_claims("lease-test", "task") == claims
    assert (await tasks.get_task("lease-test", "task"))["state"] == "in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["epoch", "expired", "wrong_agent", "ended"])
async def test_invalid_session_lease_is_rejected_before_claim_commit(lease_case, invalid):
    sessions, tasks, first, now = lease_case
    fields = lease(first)
    owner = "la_one"
    if invalid == "epoch":
        fields["session_epoch"] += 1
    elif invalid == "expired":
        fields["hard_expires_at"] = utc_text(now - timedelta(seconds=1))
    elif invalid == "wrong_agent":
        owner = "la_two"
    else:
        await stop(sessions, first, now + timedelta(seconds=1))
    with pytest.raises(PersistentStoreError):
        await tasks.claim_owner("lease-test", "task", ClaimOwner.logical_agent(owner), lease=fields)
    assert await tasks.active_claims("lease-test", "task") == []
    assert (await tasks.get_task("lease-test", "task"))["revision"] == 1

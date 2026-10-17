from datetime import timedelta

import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.persistent_agents import PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore


@pytest_asyncio.fixture
async def lease_case(tmp_path):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    sessions = WorkWindowStore(repo.path, authority_node_id="home")
    now = utc_now()
    for agent, selector in [("la_one", "ABCD"), ("la_two", "EFGH")]:
        await sessions.create_slot(
            agent,
            agent,
            selector,
            authority_node_id="home",
            initial_arm_duration_seconds=1380,
            now=utc_text(now),
        )
    session = await sessions.start_managed_session(
        "la_one",
        role="executor",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=now,
    )
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
    await sessions.begin_managed_stop(*args, principal_id="principal", reason=reason, now=now)
    return await sessions.finish_managed_stop(*args, principal_id="principal", now=now)


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

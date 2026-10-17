"""Independent claim lifetime regressions on isolated temporary SQLite stores."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.persistent_agents import ClaimOwner
from terminal_mcp.storage.persistent_agents import PersistentStoreError
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.storage.work_windows import WorkWindowStore

NS = "lease-review"
TASK = "isolated"
REMOTE = "la_remote_review"
OTHER = "la_other_review"


@pytest_asyncio.fixture
async def remote_case(tmp_path):
    output = tmp_path / "output.sqlite3"
    repo = SqliteRepository(tmp_path / "state.sqlite3", output)
    await repo.initialize()
    slots = WorkWindowStore(repo.path, authority_node_id="local")
    now = utc_now()
    for agent, selector in ((REMOTE, "ABCD"), (OTHER, "EFGH")):
        await slots.create_slot(
            agent,
            agent,
            selector,
            authority_node_id="remote",
            initial_arm_duration_seconds=1380,
            now=utc_text(now),
        )
    tasks = TaskStore(repo.path)
    await tasks.create_task(
        NS, TASK, "temporary lease test", cooperative=True, checkpoint={"retain": ["handoff", 7]}
    )
    return SimpleNamespace(path=repo.path, output=output, tasks=tasks, now=now)


def lease(case, *, session="ws_remote_review_a", epoch=1, expires=None):
    return {
        "work_session_id": session,
        "session_epoch": epoch,
        "hard_expires_at": utc_text(expires or case.now + timedelta(seconds=120)),
    }


async def close_remote(store, fields, *, now, reason="session_end"):
    identity = (REMOTE, fields["work_session_id"], fields["session_epoch"])
    await store.fence_claim_session(*identity, reason=reason, now=utc_text(now))
    return await store.release_work_session_claims(*identity, reason=reason, now=utc_text(now))


async def reconcile(store, now):
    """Exercise the persisted maintenance inputs and transactional cleanup API."""
    count = 0
    for identity in await store.expired_claim_sessions(local_authority="local", now=utc_text(now)):
        await store.fence_claim_session(**identity, reason="hard_duration", now=utc_text(now))
        count += await store.release_work_session_claims(
            **identity, reason="hard_duration", now=utc_text(now)
        )
    return count


@pytest.mark.asyncio
async def test_remote_fence_rejects_late_claim_before_any_claim_existed(remote_case):
    case = remote_case
    fields = lease(case)
    assert await close_remote(case.tasks, fields, now=case.now) == 0
    with pytest.raises(PersistentStoreError):
        await case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(REMOTE), lease=fields)
    assert await case.tasks.active_claims(NS, TASK) == []
    assert (await case.tasks.get_task(NS, TASK))["revision"] == 1


@pytest.mark.asyncio
async def test_claim_waiting_on_database_lock_rechecks_expiry_at_commit(remote_case, monkeypatch):
    from contextlib import asynccontextmanager

    from terminal_mcp.storage import claim_leases

    case = remote_case
    clock = [case.now]
    fields = lease(case, expires=case.now + timedelta(seconds=1))
    monkeypatch.setattr(claim_leases, "utc_now", lambda: clock[0])
    waiting = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_connect = case.tasks._connect

    @asynccontextmanager
    async def traced_connection():
        async with original_connect() as db:
            await db.set_trace_callback(
                lambda sql: (
                    loop.call_soon_threadsafe(waiting.set) if sql == "BEGIN IMMEDIATE" else None
                )
            )
            yield db

    monkeypatch.setattr(case.tasks, "_connect", traced_connection)
    async with aiosqlite.connect(case.path) as blocker:
        await blocker.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(
            case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(REMOTE), lease=fields)
        )
        await asyncio.wait_for(waiting.wait(), timeout=1)
        clock[0] += timedelta(seconds=2)
        await blocker.commit()
    with pytest.raises(PersistentStoreError):
        await pending
    assert await case.tasks.active_claims(NS, TASK) == []


@pytest.mark.asyncio
async def test_late_remote_cleanup_preserves_successor_claim(remote_case):
    case = remote_case
    original = lease(case)
    await case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(REMOTE), lease=original)
    assert await close_remote(case.tasks, original, now=case.now) == 1
    successor = lease(case, session="ws_remote_review_b", epoch=2)
    await case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(REMOTE), lease=successor)
    claims = await case.tasks.active_claims(NS, TASK)
    before = await case.tasks.get_task(NS, TASK)
    assert await close_remote(case.tasks, original, now=case.now + timedelta(seconds=1)) == 0
    assert await case.tasks.active_claims(NS, TASK) == claims
    assert (await case.tasks.get_task(NS, TASK))["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_expired_remote_lease_release_preserves_cooperative_durable_owner(remote_case):
    case = remote_case
    fields = lease(case)
    await case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(REMOTE), lease=fields)
    await case.tasks.claim_owner(NS, TASK, ClaimOwner.logical_agent(OTHER))
    released = await case.tasks.release_work_session_claims(
        REMOTE,
        fields["work_session_id"],
        fields["session_epoch"],
        reason="hard_duration",
        now=utc_text(case.now + timedelta(seconds=121)),
    )
    assert released == 1
    (remaining,) = await case.tasks.active_claims(NS, TASK)
    assert remaining["owner_id"] == OTHER

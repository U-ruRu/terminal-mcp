from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest_asyncio

from terminal_mcp.application.managed_sessions import ManagedSessionApplication
from terminal_mcp.application.window_recovery import ManagedWindowRecovery
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.work_windows import WindowLifecycle
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Fence:
    def __init__(self, store):
        self.store = store
        self.blocked = set()
        self.failed = set()
        self.calls = []
        self.hang = False
        self.entered = asyncio.Event()
        self.active = 0
        self.maximum = 0

    async def revoke_session(self, agent, session_id, epoch, *, reason):
        assert (await self.store.get_work_session(session_id)).state == "stopping"
        self.calls.append((agent, session_id, epoch))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        try:
            await asyncio.sleep(0)
            if self.hang:
                await asyncio.sleep(30)
            if agent in self.failed:
                raise OSError("private-error-content")
            return [{"kind": "executor_offline"}] if agent in self.blocked else []
        finally:
            self.active -= 1


@pytest_asyncio.fixture
async def env(tmp_path):
    path = tmp_path / "state.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    store = WorkWindowStore(path, authority_node_id="home")
    clock = [T0]
    fence = Fence(store)
    app = ManagedSessionApplication(
        store, object(), fence, clock=lambda: clock[0], fence_timeout_seconds=0.1
    )
    recovery = ManagedWindowRecovery(
        store, app, clock=lambda: clock[0], batch_size=4, concurrency=2, item_timeout_seconds=0.3
    )
    return SimpleNamespace(
        repo=repo, store=store, clock=clock, fence=fence, app=app, recovery=recovery
    )


async def create(env, number=1):
    agent = f"la_{number:04}"
    await env.store.create_slot(
        agent,
        agent,
        f"{number:04}",
        authority_node_id="home",
        initial_arm_duration_seconds=1380,
        now=utc_text(T0),
    )
    return await env.store.start_managed_session(
        agent,
        role="executor",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=T0,
    )


async def queue_row(env, window_id):
    async with env.store._connect("test_recovery_row") as db:
        return await (
            await db.execute(
                "SELECT next_check_at,attempts,lease_token,lease_expires_at,last_error_code "
                "FROM logical_agent_window_recovery WHERE work_window_id=?",
                (window_id,),
            )
        ).fetchone()


async def test_recovery_drains_a_due_window(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(seconds=1380)
    batch = await env.recovery.tick()
    assert (batch.claimed, batch.completed, batch.deferred) == (1, 1, 0)
    window = await env.store.current_window(first.window.logical_agent_id)
    assert window.lifecycle is WindowLifecycle.COOLDOWN
    assert window.rearm_at == T0 + timedelta(seconds=1560)
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "expired"


async def test_future_window_has_no_due_recovery(env):
    await create(env)
    assert await env.store.claim_window_recovery(now=T0) == []


async def test_parallel_instances_claim_disjoint_durable_leases(env):
    await create(env, 1)
    await create(env, 2)
    now = T0 + timedelta(seconds=2000)
    other = WorkWindowStore(env.store.path, authority_node_id="home")
    first, second = await asyncio.gather(
        env.store.claim_window_recovery(limit=1, now=now),
        other.claim_window_recovery(limit=1, now=now),
    )
    assert len(first) == len(second) == 1
    assert first[0].window.work_window_id != second[0].window.work_window_id


async def test_stale_completion_preserves_reclaimed_lease(env):
    first = await create(env)
    now = T0 + timedelta(seconds=2000)
    old = (await env.store.claim_window_recovery(now=now))[0]
    new = (await env.store.claim_window_recovery(now=now + timedelta(seconds=30)))[0]
    assert not await env.store.finish_window_recovery(old, now=now + timedelta(seconds=31))
    assert (await queue_row(env, first.window.work_window_id))[2] == new.token


async def test_recovery_uses_deadline_updated_after_lease_claim(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(seconds=1300)
    await env.store.begin_managed_stop(
        first.window.logical_agent_id,
        first.session.work_session_id,
        1,
        principal_id="principal",
        now=env.clock[0],
    )
    lease = (await env.store.claim_window_recovery(now=env.clock[0]))[0]
    change = await env.store.change_window(
        first.window.logical_agent_id,
        expected_revision=1,
        principal_id="operator",
        delta_seconds=1200,
        now=env.clock[0],
    )
    assert await env.app.reconcile_window(lease.window)
    assert await env.store.current_window(first.window.logical_agent_id) == change.current


async def test_failed_end_recovers_without_expiring_or_replacing_work_window(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(seconds=20)
    await env.store.begin_managed_stop(
        first.window.logical_agent_id,
        first.session.work_session_id,
        1,
        principal_id="principal",
        now=env.clock[0],
    )
    batch = await env.recovery.tick()
    assert batch.completed == 1
    assert await env.store.current_window(first.window.logical_agent_id) == first.window
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "ended"
    assert (await queue_row(env, first.window.work_window_id))[0] == utc_text(
        first.window.hard_expires_at
    )


async def test_foreign_authority_cannot_claim_local_recovery(env):
    await create(env)
    foreign = WorkWindowStore(env.store.path, authority_node_id="other")
    assert await foreign.claim_window_recovery(now=T0 + timedelta(seconds=2000)) == []

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.managed_sessions import ManagedSessionApplication
from terminal_mcp.application.window_recovery import ManagedWindowRecovery
from terminal_mcp.core.managed_sessions import ManagedSessionError
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


async def test_no_agent_calls_are_needed_to_expire_and_drain_window(env):
    first = await create(env)
    assert (await env.recovery.tick()).claimed == 0
    env.clock[0] = T0 + timedelta(seconds=1380)
    batch = await env.recovery.tick()
    assert (batch.claimed, batch.completed, batch.deferred) == (1, 1, 0)
    window = await env.store.current_window(first.window.logical_agent_id)
    assert window.lifecycle is WindowLifecycle.COOLDOWN
    assert window.expired_at == T0 + timedelta(seconds=1380)
    assert window.rearm_at == T0 + timedelta(seconds=1560)
    assert await queue_row(env, window.work_window_id) is None
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "expired"


async def test_restart_after_long_offline_preserves_deadline_and_cooldown(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(days=90)
    reopened = WorkWindowStore(env.store.path, authority_node_id="home")
    app = ManagedSessionApplication(reopened, object(), env.fence, clock=lambda: env.clock[0])
    recovery = ManagedWindowRecovery(reopened, app, clock=lambda: env.clock[0])
    assert (await recovery.tick()).completed == 1
    window = await reopened.current_window(first.window.logical_agent_id)
    assert window.rearm_at == T0 + timedelta(seconds=1560)
    successor = await reopened.start_managed_session(
        first.window.logical_agent_id,
        role="executor",
        contract_version=1,
        principal_id="principal",
        auth_generation=1,
        now=env.clock[0],
    )
    assert successor.session.session_epoch == 2
    assert successor.window.hard_expires_at == env.clock[0] + timedelta(seconds=1380)


async def test_batch_and_concurrency_are_bounded_and_offline_agent_does_not_starve_peers(env):
    windows = [await create(env, i) for i in range(1, 12)]
    env.fence.blocked.add(windows[0].window.logical_agent_id)
    env.clock[0] = T0 + timedelta(seconds=2000)
    batches = [await env.recovery.tick() for _ in range(4)]
    assert max(batch.claimed for batch in batches) <= 4
    assert sum(batch.completed for batch in batches) == 10
    assert sum(batch.deferred for batch in batches) == 1
    assert env.fence.maximum <= 2
    assert len({entry[0] for entry in env.fence.calls}) == 11
    pending = await queue_row(env, windows[0].window.work_window_id)
    assert pending[0] == utc_text(env.clock[0] + timedelta(seconds=3))
    assert pending[1] == 1 and pending[4] == "execution_pending"
    assert (await env.recovery.tick()).claimed == 0
    env.clock[0] += timedelta(seconds=3)
    env.fence.blocked.clear()
    assert (await env.recovery.tick()).completed == 1


async def test_parallel_instances_claim_disjoint_durable_leases(env):
    for i in range(1, 9):
        await create(env, i)
    now = T0 + timedelta(seconds=2000)
    other = WorkWindowStore(env.store.path, authority_node_id="home")
    first, second = await asyncio.gather(
        env.store.claim_window_recovery(limit=4, now=now),
        other.claim_window_recovery(limit=4, now=now),
    )
    assert len(first) == len(second) == 4
    assert not {lease.window.work_window_id for lease in first} & {
        lease.window.work_window_id for lease in second
    }
    assert await other.claim_window_recovery(now=now) == []


async def test_abandoned_lease_is_reclaimed_and_old_completion_cannot_release_new_lease(env):
    first = await create(env)
    now = T0 + timedelta(seconds=2000)
    old = (await env.store.claim_window_recovery(now=now))[0]
    assert not await env.store.claim_window_recovery(now=now + timedelta(seconds=29))
    new = (await env.store.claim_window_recovery(now=now + timedelta(seconds=30)))[0]
    assert old.token != new.token and new.attempt == 2
    assert not await env.store.finish_window_recovery(old, now=now + timedelta(seconds=31))
    row = await queue_row(env, first.window.work_window_id)
    assert row[2] == new.token


async def test_operator_extension_wins_over_an_old_recovery_ticket(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(seconds=1300)
    # A failed End schedules an early cleanup lease while the window is still open.
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
    assert await env.store.finish_window_recovery(lease, now=env.clock[0])
    row = await queue_row(env, first.window.work_window_id)
    assert row[0] == utc_text(change.current.hard_expires_at)
    assert (
        await env.store.current_window(first.window.logical_agent_id)
    ).lifecycle is WindowLifecycle.OPEN
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "ended"


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


async def test_cancelled_recovery_keeps_revocation_and_leases_recover_after_expiry(env):
    first = await create(env)
    env.clock[0] = T0 + timedelta(seconds=2000)
    env.fence.hang = True
    env.app.fence_timeout_seconds = 5
    env.recovery.item_timeout_seconds = 5
    task = asyncio.create_task(env.recovery.tick())
    await asyncio.wait_for(env.fence.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "stopping"
    assert (
        await env.store.current_window(first.window.logical_agent_id)
    ).lifecycle is WindowLifecycle.EXPIRED
    assert not await env.store.claim_window_recovery(now=env.clock[0])
    env.clock[0] += timedelta(seconds=31)
    env.fence.hang = False
    assert (await env.recovery.tick()).completed == 1


async def test_bootstrap_repairs_missing_queue_rows_without_resetting_a_live_lease(env):
    first = await create(env)
    async with env.store._transaction("test_drop_recovery") as db:
        await db.execute("DELETE FROM logical_agent_window_recovery")
    await env.repo.initialize()
    assert (await queue_row(env, first.window.work_window_id))[0] == utc_text(
        first.window.hard_expires_at
    )
    now = T0 + timedelta(seconds=2000)
    lease = (await env.store.claim_window_recovery(now=now))[0]
    await env.repo.initialize()
    assert (await queue_row(env, first.window.work_window_id))[2] == lease.token


async def test_foreign_authority_cannot_claim_or_complete_local_recovery(env):
    await create(env)
    foreign = WorkWindowStore(env.store.path, authority_node_id="other")
    now = T0 + timedelta(seconds=2000)
    assert not await foreign.claim_window_recovery(now=now)
    lease = (await env.store.claim_window_recovery(now=now))[0]
    with pytest.raises(ManagedSessionError, match="authority_unavailable"):
        await foreign.finish_window_recovery(lease, now=now)


async def test_real_durable_command_fence_blocks_until_command_cancelled(env):
    first = await create(env)
    command = await env.repo.create(
        "printf bounded-recovery",
        queue_id=1,
        agent_id=first.window.logical_agent_id,
        logical_agent_id=first.window.logical_agent_id,
        work_session_id=first.session.work_session_id,
        session_epoch=1,
        command_type="persistent_run",
    )
    env.clock[0] = T0 + timedelta(seconds=2000)
    assert (await env.recovery.tick()).deferred == 1
    assert (await env.store.get_work_session(first.session.work_session_id)).state == "stopping"
    await env.repo.cancel_queued(command.cmd_hash)
    env.clock[0] += timedelta(seconds=3)
    assert (await env.recovery.tick()).completed == 1


@pytest.mark.parametrize("limit", [0, 33, -1, True, 1.0, "1"])
async def test_recovery_batch_limit_is_strict(env, limit):
    with pytest.raises(ManagedSessionError, match="recovery_limit_invalid"):
        await env.store.claim_window_recovery(limit=limit)


@pytest.mark.parametrize("lease_seconds", [0, 31, -1, True, 1.0])
async def test_recovery_lease_duration_is_strict(env, lease_seconds):
    with pytest.raises(ManagedSessionError, match="recovery_lease_invalid"):
        await env.store.claim_window_recovery(lease_seconds=lease_seconds)


def test_invalid_controller_bounds_are_rejected():
    for kwargs in (
        {"batch_size": 0},
        {"batch_size": 33},
        {"batch_size": True},
        {"concurrency": 0},
        {"concurrency": 5},
        {"concurrency": True},
        {"item_timeout_seconds": 6},
        {"item_timeout_seconds": float("nan")},
        {"batch_size": 32, "concurrency": 1, "item_timeout_seconds": 5},
    ):
        with pytest.raises(ValueError):
            ManagedWindowRecovery(object(), object(), **kwargs)

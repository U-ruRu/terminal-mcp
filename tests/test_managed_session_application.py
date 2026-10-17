from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.managed_sessions import ManagedSessionApplication, ManagedSlotGrant
from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.core.persistent_admission import PersistentAdmissionError
from terminal_mcp.core.work_windows import WindowLifecycle, WindowPhase
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.work_windows import WorkWindowStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def actor(*, principal="principal-a", role="executor", scopes=None):
    return ActorContext(
        principal_id=principal,
        credential_id="private-credential",
        auth_generation=1,
        auth_mode="oauth",
        provider="example",
        node_id="home",
        endpoint_role=role,
        scopes=frozenset({"terminal:read", "terminal:execute"} if scopes is None else scopes),
    ).with_agent("la_one", "home")


class Authorizer:
    def __init__(self):
        self.calls = []
        self.deny = False
        self.operator = True
        self.authority_epoch = 1

    async def authorize(self, context, agent, operation):
        self.calls.append((context, agent, operation))
        if self.deny:
            raise ManagedSessionError("access_denied")
        return ManagedSlotGrant(
            logical_agent_id=agent,
            authority_node_id="home",
            authority_epoch=self.authority_epoch,
            public_name="Stable-Agent",
            principal_id=context.principal_id,
            auth_generation=context.auth_generation,
            operator=self.operator and context.endpoint_role == "operator",
        )


class Fence:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.blockers = []
        self.error = False
        self.hang = False
        self.entered = asyncio.Event()

    async def revoke_session(self, agent, session_id, epoch, *, reason):
        current = await self.store.get_work_session(session_id)
        assert current.state == "stopping", "authority must be revoked BEFORE execution fence"
        self.calls.append((agent, session_id, epoch, reason))
        self.entered.set()
        if self.error:
            raise OSError("private runtime diagnostics must not become a public error")
        if self.hang:
            await asyncio.Future()
        return self.blockers


@pytest_asyncio.fixture
async def env(tmp_path):
    repo = SqliteRepository(tmp_path / "state.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    store = WorkWindowStore(repo.path, authority_node_id="home")
    await store.create_slot(
        "la_one",
        "display",
        "ABCD",
        authority_node_id="home",
        initial_arm_duration_seconds=1380,
        now=utc_text(T0),
    )
    authorizer = Authorizer()
    fence = Fence(store)
    clock = [T0]
    app = ManagedSessionApplication(
        store, authorizer, fence, clock=lambda: clock[0], fence_timeout_seconds=0.05
    )
    return SimpleNamespace(
        repo=repo, store=store, auth=authorizer, fence=fence, clock=clock, app=app
    )


async def test_start_binds_actor_to_current_server_session(env):
    started = await env.app.start(actor())
    assert started.actor.work_session_id == started.snapshot.session.work_session_id
    assert started.actor.principal_id == "principal-a"
    receipt = started.receipt()
    assert receipt["public_name"] == "Stable-Agent"
    assert receipt["remaining_seconds"] == 1380
    assert receipt["role"] == "executor"


async def test_expiry_fences_before_returning_session_expired(env):
    started = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1380)
    with pytest.raises(ManagedSessionError, match="session_expired") as error:
        await env.app.authorize_operation(started.actor, ManagedOperation.COMMAND_RUN)
    assert error.value.return_to_chat
    assert len(env.fence.calls) == 1
    assert (await env.store.get_work_session(started.actor.work_session_id)).state == "expired"
    assert (await env.store.current_window("la_one")).lifecycle is WindowLifecycle.COOLDOWN


@pytest.mark.parametrize("mode", ["blocked", "error", "timeout"])
async def test_committed_expiry_is_reported_with_pending_cleanup_not_retried_mutation(env, mode):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=100)
    env.fence.blockers = [{"kind": "detached_command"}] if mode == "blocked" else []
    env.fence.error = mode == "error"
    env.fence.hang = mode == "timeout"
    result = await env.app.change_window(
        actor(role="operator"), "la_one", expected_revision=1, duration_seconds=50
    )
    assert result.cleanup_pending
    assert result.change.current.window_revision == 2
    assert result.change.current.lifecycle is WindowLifecycle.EXPIRED
    assert (await env.store.get_work_session(first.actor.work_session_id)).state == "stopping"
    assert (await env.store.current_window("la_one")).window_revision == 2


async def test_cancelled_cleanup_never_restores_execution_authority(env):
    first = await env.app.start(actor())
    env.fence.hang = True
    env.app.fence_timeout_seconds = 5.0
    stopping = asyncio.create_task(env.app.end(first.actor))
    await asyncio.wait_for(env.fence.entered.wait(), timeout=1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert (await env.store.get_work_session(first.actor.work_session_id)).state == "stopping"
    assert await env.store.current_window("la_one") == first.snapshot.window


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("authority_epoch", 2, "session_authority_stale"),
        ("auth_generation", 2, "auth_generation_mismatch"),
        ("state", "suspended", "session_not_active"),
    ],
)
async def test_gate_rechecks_current_slot_authority_in_same_snapshot(env, field, value, code):
    first = await env.app.start(actor())
    async with env.store._transaction("test_authority_changed") as db:
        await db.execute(
            f"UPDATE logical_agents SET {field}=? WHERE logical_agent_id=?", (value, "la_one")
        )
    with pytest.raises(ManagedSessionError, match=code):
        await env.app.authorize_operation(first.actor, ManagedOperation.TASK_CHECKPOINT)


async def test_gate_rejects_current_session_deadline_inconsistent_with_window(env):
    first = await env.app.start(actor())
    async with env.store._transaction("test_deadline_tamper") as db:
        await db.execute(
            "UPDATE logical_agent_work_sessions SET hard_expires_at=? WHERE work_session_id=?",
            (utc_text(T0 + timedelta(seconds=9999)), first.actor.work_session_id),
        )
    with pytest.raises(ManagedSessionError, match="session_binding_invalid"):
        await env.app.authorize_operation(first.actor, ManagedOperation.COMMAND_RUN)


async def test_operator_can_end_current_managed_session_without_impersonating_principal(env):
    started = await env.app.start(actor())
    operator = actor(principal="operator-principal", role="operator")
    result = await env.app.operator_end(operator, "la_one")
    assert result["ok"] is True
    assert result["session_state"] == "inactive"
    assert result["work_session_id"] == started.snapshot.session.work_session_id
    ended = await env.store.get_work_session(started.snapshot.session.work_session_id)
    assert ended.state == "ended"
    assert ended.end_reason == "operator_end"


async def test_read_only_principal_cannot_start_execution(env):
    with pytest.raises(PersistentAdmissionError, match="persistent_scope_required"):
        await env.app.start(actor(scopes={"terminal:read"}))
    assert await env.store.active_session_for_slot("la_one") is None


async def test_operator_endpoint_still_requires_independent_grant(env):
    await env.app.start(actor())
    env.auth.operator = False
    with pytest.raises(ManagedSessionError, match="operator_role_required"):
        await env.app.change_window(
            actor(role="operator"), "la_one", expected_revision=1, delta_seconds=10
        )
    assert (await env.store.current_window("la_one")).window_revision == 1


async def test_gate_fences_authorizer_epoch_that_does_not_match_bound_session(env):
    first = await env.app.start(actor())
    env.auth.authority_epoch = 2
    with pytest.raises(ManagedSessionError, match="session_authority_stale") as error:
        await env.app.authorize_operation(first.actor, ManagedOperation.TASK_CHECKPOINT)
    assert error.value.return_to_chat


async def test_draining_window_allows_repeated_start_of_active_session(env):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1320)
    repeated = await env.app.start(actor())
    assert repeated.snapshot.session.work_session_id == first.snapshot.session.work_session_id
    assert repeated.lifecycle.phase is WindowPhase.DRAINING


async def test_draining_window_blocks_new_session_after_end(env):
    await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1320)
    await env.app.end(actor())
    with pytest.raises(ManagedSessionError, match="session_draining") as blocked:
        await env.app.start(actor())
    assert blocked.value.return_to_chat
    assert await env.store.active_session_for_slot("la_one") is None

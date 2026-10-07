from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.managed_sessions import ManagedSessionApplication, ManagedSlotGrant
from terminal_mcp.core.managed_sessions import (
    FINALIZATION_OPERATIONS,
    OPERATOR_OPERATIONS,
    ManagedOperation,
    ManagedSessionError,
    decide_session_operation,
)
from terminal_mcp.core.orchestration import parse_utc, utc_text
from terminal_mcp.core.persistent_admission import PersistentAdmissionError
from terminal_mcp.core.persistent_execution import PersistentExecutionFence
from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowLifecycle,
    WindowPhase,
    WorkWindow,
)
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
            await asyncio.sleep(30)
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


@pytest.mark.parametrize(
    "operation", [op for op in ManagedOperation if op not in OPERATOR_OPERATIONS]
)
def test_pure_policy_exhaustively_classifies_draining(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1320))
    assert decision.phase is WindowPhase.DRAINING
    assert decision.remaining_seconds == 60
    assert decision.allowed == (operation in FINALIZATION_OPERATIONS)
    assert decision.return_to_chat
    assert decision.code == (None if decision.allowed else "session_draining")


@pytest.mark.parametrize(
    "operation", [op for op in ManagedOperation if op not in OPERATOR_OPERATIONS]
)
def test_pure_policy_expiry_never_admits_new_work(operation):
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    decision = decide_session_operation(window, operation, T0 + timedelta(seconds=1380))
    assert decision.remaining_seconds == 0
    assert decision.return_to_chat
    assert decision.allowed == (
        operation in {ManagedOperation.SESSION_END, ManagedOperation.SESSION_STATUS}
    )


def test_unknown_and_operator_operations_do_not_bypass_agent_lifecycle():
    window = WorkWindow.open("window", "agent", T0, SlotSessionPolicy())
    for operation in ("new.tool", "task.done", ManagedOperation.OPERATOR_WINDOW):
        with pytest.raises(ManagedSessionError, match="operation_not_allowed"):
            decide_session_operation(window, operation, T0)


async def test_identity_and_verified_authorization_are_independent(env):
    with pytest.raises(ManagedSessionError, match="persistent_auth_required"):
        await env.app.start(ActorContext(logical_agent_id="la_one", provider="example"))
    assert not env.auth.calls
    env.auth.deny = True
    with pytest.raises(ManagedSessionError, match="access_denied"):
        await env.app.start(actor())
    assert await env.store.active_session_for_slot("la_one") is None
    assert await env.store.current_window("la_one") is None


async def test_read_only_admission_cannot_start_or_operator_mutate(env):
    with pytest.raises(PersistentAdmissionError, match="persistent_scope_required"):
        await env.app.start(actor(scopes={"terminal:read"}))
    with pytest.raises(PersistentAdmissionError, match="persistent_scope_required"):
        await env.app.change_policy(
            actor(role="operator", scopes={"terminal:read"}),
            "la_one",
            SlotSessionPolicy(),
            expected_revision=1,
        )
    assert not env.auth.calls


async def test_start_resolves_session_server_side_and_receipt_is_compact(env):
    unresolved = actor()
    assert unresolved.logical_agent_id == "la_one" and unresolved.work_session_id is None
    started = await env.app.start(unresolved)
    again = await env.app.start(unresolved)
    assert started.snapshot.session == again.snapshot.session
    assert started.actor.work_session_id == started.snapshot.session.work_session_id
    assert started.actor.principal_id == unresolved.principal_id
    receipt = started.receipt()
    assert receipt["public_name"] == "Stable-Agent"
    assert receipt["remaining_seconds"] == 1380
    assert receipt["role"] == "executor" and receipt["contract_version"] == 1
    text = json.dumps(receipt)
    assert len(text.encode()) < 700
    assert "principal-a" not in text and "private-credential" not in text
    assert "logical_agent_id" not in receipt and "provider" not in receipt


async def test_gate_carries_current_warning_and_rejects_new_work_when_draining(env):
    started = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1200)
    permitted = await env.app.authorize_operation(started.actor, ManagedOperation.COMMAND_RUN)
    assert permitted.lifecycle.phase is WindowPhase.WARNING
    assert permitted.receipt()["remaining_seconds"] == 180
    env.clock[0] = T0 + timedelta(seconds=1320)
    for operation in (
        ManagedOperation.COMMAND_RUN,
        ManagedOperation.TASK_CREATE,
        ManagedOperation.TASK_CLAIM,
        ManagedOperation.MESSAGE_SEND,
    ):
        with pytest.raises(ManagedSessionError, match="session_draining") as error:
            await env.app.authorize_operation(started.actor, operation)
        assert error.value.return_to_chat
    for operation in (
        ManagedOperation.COMMAND_READ,
        ManagedOperation.COMMAND_CANCEL,
        ManagedOperation.TASK_CHECKPOINT,
        ManagedOperation.TASK_DONE,
        ManagedOperation.TASK_RELEASE,
        ManagedOperation.MESSAGE_ACK,
        ManagedOperation.MESSAGE_REPLY,
    ):
        result = await env.app.authorize_operation(started.actor, operation)
        assert result.lifecycle.allowed


async def test_expiry_fences_before_returning_session_expired(env):
    started = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1380)
    with pytest.raises(ManagedSessionError, match="session_expired") as error:
        await env.app.authorize_operation(started.actor, ManagedOperation.COMMAND_RUN)
    assert error.value.return_to_chat
    assert len(env.fence.calls) == 1
    assert (await env.store.get_work_session(started.actor.work_session_id)).state == "expired"
    assert (await env.store.current_window("la_one")).lifecycle is WindowLifecycle.COOLDOWN


async def test_normal_end_start_does_not_buy_a_new_budget(env):
    first = await env.app.start(actor())
    env.clock[0] += timedelta(seconds=20)
    ended = await env.app.end(first.actor)
    assert ended["session_state"] == "inactive"
    second = await env.app.start(actor())
    assert second.snapshot.window == first.snapshot.window
    assert second.actor.session_epoch == first.actor.session_epoch + 1
    assert second.receipt()["remaining_seconds"] == 1360
    # Late replay using the old server-bound session cannot stop its successor.
    await env.app.end(first.actor)
    assert (await env.store.get_work_session(second.actor.work_session_id)).state == "active"


async def test_independent_grant_cannot_reuse_another_principals_session(env):
    first = await env.app.start(actor())
    wrong = replace(first.actor, principal_id="principal-b")
    with pytest.raises(ManagedSessionError, match="session_principal_mismatch"):
        await env.app.authorize_operation(wrong, ManagedOperation.TASK_CHECKPOINT)
    with pytest.raises(ManagedSessionError, match="session_principal_mismatch"):
        await env.app.end(wrong)
    assert (await env.store.get_work_session(first.actor.work_session_id)).state == "active"


async def test_operator_duration_changes_recompute_phase_without_restarting_session(env):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1320)
    changed = await env.app.change_window(
        actor(role="operator"), "la_one", expected_revision=1, delta_seconds=1200
    )
    assert changed.change.state is WindowPhase.ACTIVE and not changed.cleanup_pending
    authorized = await env.app.authorize_operation(first.actor, ManagedOperation.COMMAND_RUN)
    assert authorized.lifecycle.phase is WindowPhase.ACTIVE
    assert authorized.actor.work_session_id == first.actor.work_session_id
    assert authorized.receipt()["remaining_seconds"] == 1260


async def test_operator_requires_both_endpoint_role_and_independent_grant(env):
    await env.app.start(actor())
    with pytest.raises(ManagedSessionError, match="operator_role_required"):
        await env.app.change_window(actor(), "la_one", expected_revision=1, delta_seconds=10)
    env.auth.operator = False
    with pytest.raises(ManagedSessionError, match="operator_role_required"):
        await env.app.change_window(
            actor(role="operator"), "la_one", expected_revision=1, delta_seconds=10
        )
    assert (await env.store.current_window("la_one")).window_revision == 1


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
    with pytest.raises(ManagedSessionError, match="session_stopping") as error:
        await env.app.start(actor())
    assert error.value.return_to_chat


async def test_failed_end_fence_is_durable_and_retry_finishes_without_reset(env):
    first = await env.app.start(actor())
    env.fence.error = True
    result = await env.app.end(first.actor)
    assert result["session_state"] == "stopping" and result["cleanup_pending"]
    env.fence.error = False
    result = await env.app.end(first.actor)
    assert result["session_state"] == "inactive"
    assert await env.store.current_window("la_one") == first.snapshot.window


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


async def test_real_queued_execution_is_cancelled_through_existing_execution_fence(env):
    started = await env.app.start(actor())
    command = await env.repo.create(
        "printf queued-test",
        queue_id=2,
        agent_id="la_one",
        logical_agent_id="la_one",
        work_session_id=started.actor.work_session_id,
        session_epoch=started.actor.session_epoch,
        command_type="persistent_run",
    )
    env.app.execution_fence = PersistentExecutionFence(env.repo, object(), object())
    ended = await env.app.end(started.actor)
    assert ended["session_state"] == "inactive"
    assert (await env.repo.get(command.cmd_hash)).status == "cancelled"


async def test_restart_expired_window_reconciles_and_preserves_original_rearm_time(env):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=2000)
    new_app = ManagedSessionApplication(env.store, env.auth, env.fence, clock=lambda: env.clock[0])
    second = await new_app.start(actor())
    assert second.snapshot.window.work_window_id != first.snapshot.window.work_window_id
    assert second.actor.session_epoch == 2
    assert second.receipt()["remaining_seconds"] == 1380
    assert len(env.fence.calls) == 1


def test_missing_security_ports_and_unbounded_timeouts_are_configuration_errors(env=None):
    with pytest.raises(ValueError, match="ports are required"):
        ManagedSessionApplication(object(), None, object())
    with pytest.raises(ValueError, match="ports are required"):
        ManagedSessionApplication(object(), object(), None)
    for timeout in (0, -1, 6, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="five seconds"):
            ManagedSessionApplication(object(), object(), object(), fence_timeout_seconds=timeout)


def test_agent_context_preserves_authorization_without_inventing_session():
    context = actor()
    assert context.logical_agent_id == "la_one"
    assert context.work_session_id is None and context.session_epoch is None
    assert context.admission().principal_id == "principal-a"
    for kwargs in (
        {"work_session_id": "x"},
        {"session_epoch": 1},
        {"work_session_id": "x", "session_epoch": 1},
    ):
        with pytest.raises(ValueError, match="complete"):
            ActorContext(**kwargs)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("authority_epoch", 2, "session_authority_stale"),
        ("auth_generation", 2, "auth_generation_mismatch"),
        ("state", "suspended", "session_not_active"),
    ],
)
@pytest.mark.parametrize("resolved", [False, True])
async def test_gate_rechecks_current_slot_authority_in_same_snapshot(
    env, field, value, code, resolved
):
    first = await env.app.start(actor())
    async with env.store._transaction("test_authority_changed") as db:
        await db.execute(
            f"UPDATE logical_agents SET {field}=? WHERE logical_agent_id=?", (value, "la_one")
        )
    with pytest.raises(ManagedSessionError, match=code):
        await env.app.authorize_operation(
            first.actor if resolved else actor(), ManagedOperation.TASK_CHECKPOINT
        )


@pytest.mark.parametrize(
    "operation",
    [
        ManagedOperation.COMMAND_RUN,
        ManagedOperation.TASK_CREATE,
        ManagedOperation.MESSAGE_SEND,
        ManagedOperation.CONTEXT_WRITE,
    ],
)
@pytest.mark.parametrize("resolved", [False, True])
async def test_gate_fences_stale_authority_epoch_for_new_work_surfaces(env, operation, resolved):
    first = await env.app.start(actor())
    async with env.store._transaction("test_authority_epoch_changed") as db:
        await db.execute(
            "UPDATE logical_agents SET authority_epoch=2 WHERE logical_agent_id=?", ("la_one",)
        )
    with pytest.raises(ManagedSessionError, match="session_authority_stale") as error:
        await env.app.authorize_operation(first.actor if resolved else actor(), operation)
    assert error.value.return_to_chat


async def test_gate_fences_authorizer_epoch_that_does_not_match_bound_session(env):
    first = await env.app.start(actor())
    env.auth.authority_epoch = 2
    with pytest.raises(ManagedSessionError, match="session_authority_stale") as error:
        await env.app.authorize_operation(first.actor, ManagedOperation.TASK_CHECKPOINT)
    assert error.value.return_to_chat


async def test_gate_rejects_current_session_deadline_inconsistent_with_window(env):
    first = await env.app.start(actor())
    async with env.store._transaction("test_deadline_tamper") as db:
        await db.execute(
            "UPDATE logical_agent_work_sessions SET hard_expires_at=? WHERE work_session_id=?",
            (utc_text(T0 + timedelta(seconds=9999)), first.actor.work_session_id),
        )
    with pytest.raises(ManagedSessionError, match="session_binding_invalid"):
        await env.app.authorize_operation(first.actor, ManagedOperation.COMMAND_RUN)


async def test_repeated_expired_gate_calls_keep_stable_error_after_revocation(env):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1400)
    for _ in range(2):
        with pytest.raises(ManagedSessionError, match="session_expired") as error:
            await env.app.authorize_operation(first.actor, ManagedOperation.COMMAND_RUN)
        assert error.value.return_to_chat

async def test_operator_surface_reports_policy_window_and_session_and_mutates_window(env):
    started = await env.app.start(actor())
    operator = actor(role="operator")

    status = await env.app.operator_status(operator, "la_one")
    assert status["public_name"] == "Stable-Agent"
    assert status["policy"]["default_duration_seconds"] == 1380
    assert status["window"]["remaining_seconds"] == 1380
    assert status["session"]["work_session_id"] == started.snapshot.session.work_session_id
    assert status["session"]["role"] == "executor"

    policy = await env.app.change_policy(
        operator,
        "la_one",
        SlotSessionPolicy(
            default_duration_seconds=1800,
            warning_before_expiry_seconds=240,
            draining_before_expiry_seconds=120,
            rearm_after_seconds=60,
        ),
        expected_revision=status["policy"]["revision"],
    )
    assert policy.policy.default_duration_seconds == 1800
    unchanged = await env.store.current_window("la_one")
    assert unchanged.effective_duration_seconds == 1380

    mutation = await env.app.change_window(
        operator,
        "la_one",
        expected_revision=unchanged.window_revision,
        delta_seconds=600,
    )
    assert mutation.change.current.effective_duration_seconds == 1980
    assert mutation.change.current.hard_expires_at == T0 + timedelta(seconds=1980)


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

async def test_explicit_managed_start_adopts_active_legacy_session_without_resetting_budget(env):
    _slot, legacy = await env.store.start_session(
        selector="ABCD",
        work_session_id="legacy-session",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id="home",
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    assert await env.store.current_window("la_one") is None

    started = await env.app.start(actor(role="legacy"))
    assert started.snapshot.session.work_session_id == legacy.work_session_id
    assert started.snapshot.session.session_epoch == legacy.session_epoch
    assert started.snapshot.session.hard_expires_at == legacy.hard_expires_at
    assert started.snapshot.binding.role == "legacy"
    assert started.snapshot.binding.contract_version == 1
    assert started.snapshot.window.hard_expires_at == parse_utc(legacy.hard_expires_at)
    assert started.receipt()["remaining_seconds"] == 1380

    again = await env.app.start(actor(role="legacy"))
    assert again.snapshot.session.work_session_id == legacy.work_session_id
    assert again.snapshot.window.work_window_id == started.snapshot.window.work_window_id
    assert again.snapshot.window.hard_expires_at == started.snapshot.window.hard_expires_at

async def test_draining_allows_idempotent_active_start_but_blocks_new_session_after_end(env):
    first = await env.app.start(actor())
    env.clock[0] = T0 + timedelta(seconds=1320)

    repeated = await env.app.start(actor())
    assert repeated.snapshot.session.work_session_id == first.snapshot.session.work_session_id
    assert repeated.lifecycle.phase is WindowPhase.DRAINING
    assert repeated.lifecycle.return_to_chat is True

    ended = await env.app.end(actor())
    assert ended["session_state"] == "inactive"
    assert await env.store.active_session_for_slot("la_one") is None

    with pytest.raises(ManagedSessionError, match="session_draining") as blocked:
        await env.app.start(actor())
    assert blocked.value.return_to_chat is True
    assert await env.store.active_session_for_slot("la_one") is None
    window = await env.store.current_window("la_one")
    assert window is not None and window.phase(env.clock[0]) is WindowPhase.DRAINING

async def test_legacy_adoption_in_draining_preserves_existing_session_instead_of_starting_new(env):
    _slot, legacy = await env.store.start_session(
        selector="ABCD",
        work_session_id="legacy-draining",
        expected_revision=1,
        principal_id="principal-a",
        auth_generation=1,
        authority_node_id="home",
        origin_instance_id="home",
        session_duration_seconds=1380,
        now=utc_text(T0),
    )
    env.clock[0] = T0 + timedelta(seconds=1320)

    adopted = await env.app.start(actor(role="legacy"))

    assert adopted.snapshot.created is False
    assert adopted.snapshot.session.work_session_id == legacy.work_session_id
    assert adopted.lifecycle.phase is WindowPhase.DRAINING
    assert adopted.lifecycle.return_to_chat is True

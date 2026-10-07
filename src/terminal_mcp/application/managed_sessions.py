"""Canonical managed Session Gate, independent of transport and process adapters.

Authorization and execution fencing are REQUIRED ports. Provider evidence never
creates authorization. Composition must supply the real local+Fleet fence and an
independent slot authorizer before exposing this capability to an endpoint.
Legacy compatibility continues through SessionGate until explicit cutover.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy
from terminal_mcp.core.managed_sessions import (
    OPERATOR_OPERATIONS,
    READ_OPERATIONS,
    LifecycleDecision,
    ManagedOperation,
    ManagedSessionError,
    ManagedSessionSnapshot,
    SlotPolicyRecord,
    decide_session_operation,
)
from terminal_mcp.core.orchestration import utc_now
from terminal_mcp.core.persistent_agents import WorkSessionRecord
from terminal_mcp.core.work_windows import (
    SlotSessionPolicy,
    WindowChange,
    WindowLifecycle,
    WorkWindow,
)

_LOG = logging.getLogger(__name__)


class ManagedExecutionFence(Protocol):
    async def revoke_session(
        self, logical_agent_id: str, work_session_id: str, session_epoch: int, *, reason: str
    ) -> list[dict]: ...


@dataclass(frozen=True, slots=True)
class ManagedSlotGrant:
    logical_agent_id: str
    authority_node_id: str
    authority_epoch: int
    public_name: str
    principal_id: str
    auth_generation: int
    operator: bool = False

    def __post_init__(self):
        for value, limit in (
            (self.logical_agent_id, 256),
            (self.authority_node_id, 256),
            (self.public_name, 80),
            (self.principal_id, 256),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > limit
                or any(ord(c) < 32 for c in value)
            ):
                raise ManagedSessionError("authorization_grant_invalid")
        if type(self.authority_epoch) is not int or self.authority_epoch < 1:
            raise ManagedSessionError("authorization_grant_invalid")
        if type(self.auth_generation) is not int or self.auth_generation < 1:
            raise ManagedSessionError("authorization_grant_invalid")
        if type(self.operator) is not bool:
            raise ManagedSessionError("authorization_grant_invalid")


class ManagedSessionAuthorizer(Protocol):
    async def authorize(
        self, actor: ActorContext, logical_agent_id: str, operation: ManagedOperation
    ) -> ManagedSlotGrant: ...


class ManagedSessionRepository(Protocol):
    authority_node_id: str

    async def active_session_for_slot(self, logical_agent_id: str) -> WorkSessionRecord | None: ...
    async def session_snapshot(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        require_live: bool = False,
    ) -> ManagedSessionSnapshot: ...
    async def current_window(self, logical_agent_id: str) -> WorkWindow | None: ...
    async def policy(self, logical_agent_id: str) -> SlotPolicyRecord: ...
    async def adopt_legacy_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        policy: SlotSessionPolicy | None = None,
        now: datetime | None = None,
    ) -> ManagedSessionSnapshot: ...
    async def start_managed_session(
        self,
        logical_agent_id: str,
        *,
        role: str,
        contract_version: int,
        principal_id: str,
        auth_generation: int,
        origin_instance_id: str | None = None,
        now: datetime | None = None,
    ) -> ManagedSessionSnapshot: ...
    async def begin_managed_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        reason: str = "session_end",
        now: datetime | None = None,
    ) -> WorkSessionRecord: ...
    async def finish_managed_stop(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        principal_id: str,
        now: datetime | None = None,
    ) -> WorkSessionRecord: ...
    async def expire_window(
        self, logical_agent_id: str, *, expected_revision: int, now: datetime | None = None
    ) -> WorkWindow: ...
    async def complete_window_fence(
        self, logical_agent_id: str, *, expected_revision: int, now: datetime | None = None
    ) -> WorkWindow: ...
    async def update_policy(
        self,
        logical_agent_id: str,
        policy: SlotSessionPolicy,
        *,
        expected_revision: int,
        principal_id: str,
        now: datetime | None = None,
    ) -> SlotPolicyRecord: ...
    async def change_window(
        self,
        logical_agent_id: str,
        *,
        expected_revision: int,
        principal_id: str,
        now: datetime | None = None,
        duration_seconds: int | None = None,
        delta_seconds: int | None = None,
        warning_before_expiry_seconds: int | None = None,
        draining_before_expiry_seconds: int | None = None,
    ) -> WindowChange: ...


@dataclass(frozen=True, slots=True)
class ManagedSessionAdmission:
    actor: ActorContext
    snapshot: ManagedSessionSnapshot
    public_name: str
    lifecycle: LifecycleDecision

    def receipt(self) -> dict:
        """Bounded projection: no provider identifiers, credentials or history."""
        session = self.snapshot.session
        binding = self.snapshot.binding
        result = {
            "ok": True,
            "public_name": self.public_name,
            "work_session_id": session.work_session_id,
            "session_epoch": session.session_epoch,
            "role": binding.role,
            "contract_version": binding.contract_version,
            "state": self.lifecycle.phase.value,
            "started_at": session.started_at,
            "hard_expires_at": session.hard_expires_at,
            "remaining_seconds": self.lifecycle.remaining_seconds,
        }
        if self.lifecycle.return_to_chat:
            result["return_to_chat"] = True
        return result


@dataclass(frozen=True, slots=True)
class ManagedWindowMutation:
    change: WindowChange
    cleanup_pending: bool = False


class ManagedSessionApplication:
    def __init__(
        self,
        repository: ManagedSessionRepository,
        authorizer: ManagedSessionAuthorizer,
        execution_fence: ManagedExecutionFence,
        *,
        clock: Callable[[], datetime] = utc_now,
        fence_timeout_seconds: float = 2.0,
    ):
        if authorizer is None or execution_fence is None:
            raise ValueError("authorization and execution fence ports are required")
        if (
            isinstance(fence_timeout_seconds, bool)
            or not isfinite(fence_timeout_seconds)
            or not 0 < fence_timeout_seconds <= 5
        ):
            raise ValueError("fence timeout must be positive and at most five seconds")
        self.repository = repository
        self.authorizer = authorizer
        self.execution_fence = execution_fence
        self.clock = clock
        self.fence_timeout_seconds = fence_timeout_seconds
        self.policy = CapabilityPolicy()

    async def _authorize(
        self, actor: ActorContext, operation: ManagedOperation, logical_agent_id: str | None = None
    ) -> ManagedSlotGrant:
        if not isinstance(operation, ManagedOperation):
            raise ManagedSessionError("operation_not_allowed")
        operator = operation in OPERATOR_OPERATIONS
        if operator and actor.endpoint_role != "operator":
            raise ManagedSessionError("operator_role_required")
        capability = {
            "session": "sessions",
            "command": "commands",
            "task": "tasks",
            "message": "messages",
            "context": "contexts",
            "operator": "sessions",
            "observe": "observations",
            "health": "health",
        }[operation.value.split(".")[0]]
        if not self.policy.allows(actor, capability):
            raise ManagedSessionError("capability_not_allowed")
        admission = actor.admission()
        if admission is None:
            raise ManagedSessionError("persistent_auth_required")
        admission.require("terminal:read" if operation in READ_OPERATIONS else "terminal:execute")
        agent = logical_agent_id if operator else actor.logical_agent_id
        if not agent:
            raise ManagedSessionError("identity_required")
        grant = await self.authorizer.authorize(actor, agent, operation)
        if not isinstance(grant, ManagedSlotGrant) or grant.logical_agent_id != agent:
            raise ManagedSessionError("authorization_grant_invalid")
        if operator and not grant.operator:
            raise ManagedSessionError("operator_role_required")
        if grant.authority_node_id != self.repository.authority_node_id or (
            not operator
            and actor.authority_node_id is not None
            and actor.authority_node_id != grant.authority_node_id
        ):
            raise ManagedSessionError("authority_unavailable")
        return grant

    async def _snapshot(self, actor: ActorContext, grant: ManagedSlotGrant, *, require_live=False):
        if actor.work_session_id is not None:
            return await self.repository.session_snapshot(
                grant.logical_agent_id,
                actor.work_session_id,
                actor.session_epoch,
                require_live=require_live,
            )
        session = await self.repository.active_session_for_slot(grant.logical_agent_id)
        if session is None:
            raise ManagedSessionError("session_not_found")
        return await self.repository.session_snapshot(
            grant.logical_agent_id,
            session.work_session_id,
            session.session_epoch,
            require_live=require_live,
        )

    @staticmethod
    def _owns(actor, grant, snapshot):
        if (
            snapshot.session.authority_node_id != grant.authority_node_id
            or snapshot.session.authority_epoch != grant.authority_epoch
        ):
            raise ManagedSessionError("session_authority_stale", return_to_chat=True)
        if (
            snapshot.session.auth_principal_id != grant.principal_id
            or snapshot.session.auth_generation != grant.auth_generation
        ):
            raise ManagedSessionError("session_principal_mismatch")
        snapshot.binding.require_contract(actor.endpoint_role, actor.contract_version)

    @staticmethod
    def _admitted(actor, grant, snapshot, decision):
        resolved = actor.with_identity(
            {
                "logical_agent_id": snapshot.session.logical_agent_id,
                "work_session_id": snapshot.session.work_session_id,
                "session_epoch": snapshot.session.session_epoch,
                "authority_node_id": grant.authority_node_id,
            }
        )
        return ManagedSessionAdmission(resolved, snapshot, grant.public_name, decision)

    async def ensure_agent_grant(self, actor: ActorContext, logical_agent_id: str) -> None:
        ensure = getattr(self.authorizer, "ensure_agent_grant", None)
        if not callable(ensure):
            raise ManagedSessionError("authority_unavailable")
        await ensure(actor, logical_agent_id)

    async def start(self, actor: ActorContext) -> ManagedSessionAdmission:
        grant = await self._authorize(actor, ManagedOperation.SESSION_START)
        with actor.bind():
            current = await self.repository.current_window(grant.logical_agent_id)
            if current is not None and (
                current.lifecycle is WindowLifecycle.EXPIRED
                or (
                    current.lifecycle is WindowLifecycle.OPEN
                    and self.clock() >= current.hard_expires_at
                )
            ):
                await self._safe_reconcile(current)
            try:
                snapshot = await self.repository.start_managed_session(
                    grant.logical_agent_id,
                    role=actor.endpoint_role,
                    contract_version=actor.contract_version,
                    principal_id=grant.principal_id,
                    auth_generation=grant.auth_generation,
                    origin_instance_id=actor.node_id or None,
                    now=self.clock(),
                )
            except ManagedSessionError as exc:
                if exc.code == "session_migration_required":
                    legacy = await self.repository.active_session_for_slot(grant.logical_agent_id)
                    if legacy is None:
                        raise ManagedSessionError("session_not_found") from exc
                    snapshot = await self.repository.adopt_legacy_session(
                        grant.logical_agent_id,
                        legacy.work_session_id,
                        legacy.session_epoch,
                        principal_id=grant.principal_id,
                        now=self.clock(),
                    )
                else:
                    if exc.code in {
                        "session_expired",
                        "session_stopping",
                        "window_cooldown",
                    }:
                        exc.return_to_chat = True
                    raise
            self._owns(actor, grant, snapshot)
        now = self.clock()
        if not snapshot.created and snapshot.window.phase(now).value == "draining":
            decision = LifecycleDecision(
                True,
                snapshot.window.phase(now),
                snapshot.window.remaining_seconds(now),
                True,
            )
        else:
            decision = decide_session_operation(
                snapshot.window, ManagedOperation.SESSION_START, now
            )
        if not decision.allowed:
            raise ManagedSessionError(decision.code, current=snapshot.window, return_to_chat=True)
        return self._admitted(actor, grant, snapshot, decision)

    async def authorize_operation(
        self, actor: ActorContext, operation: ManagedOperation
    ) -> ManagedSessionAdmission:
        if operation in OPERATOR_OPERATIONS or operation is ManagedOperation.SESSION_START:
            raise ManagedSessionError("operation_not_allowed")
        grant = await self._authorize(actor, operation)
        with actor.bind():
            snapshot = await self._snapshot(
                actor,
                grant,
                require_live=(
                    operation
                    not in {
                        ManagedOperation.SESSION_END,
                        ManagedOperation.SESSION_STATUS,
                    }
                ),
            )
            self._owns(actor, grant, snapshot)
            now = self.clock()
            decision = decide_session_operation(snapshot.window, operation, now)
            if decision.code == "session_expired":
                await self._safe_reconcile(snapshot.window)
                raise ManagedSessionError(
                    "session_expired", current=snapshot.window, return_to_chat=True
                )
            if not decision.allowed:
                raise ManagedSessionError(
                    decision.code, current=snapshot.window, return_to_chat=decision.return_to_chat
                )
            if snapshot.session.state != "active" and operation not in {
                ManagedOperation.SESSION_END,
                ManagedOperation.SESSION_STATUS,
            }:
                raise ManagedSessionError("session_not_active", return_to_chat=True)
            return self._admitted(actor, grant, snapshot, decision)

    async def _drain(self, session: WorkSessionRecord, reason: str) -> bool:
        try:
            async with asyncio.timeout(self.fence_timeout_seconds):
                blockers = await self.execution_fence.revoke_session(
                    session.logical_agent_id,
                    session.work_session_id,
                    session.session_epoch,
                    reason=reason,
                )
        except TimeoutError:
            return False
        except Exception as exc:
            # Authority is already revoked. A failed provider cannot turn a
            # committed end/expiry into an apparent uncommitted mutation.
            _LOG.warning("managed execution drain deferred: %s", type(exc).__name__)
            return False
        return not blockers

    async def end(self, actor: ActorContext, *, reason: str = "session_end") -> dict:
        if reason not in {"session_end", "session_interrupt"}:
            raise ManagedSessionError("invalid_request")
        grant = await self._authorize(actor, ManagedOperation.SESSION_END)
        with actor.bind():
            try:
                snapshot = await self._snapshot(actor, grant)
            except ManagedSessionError as exc:
                if exc.code == "session_not_found" and actor.work_session_id is None:
                    return {
                        "ok": True,
                        "session_state": "inactive",
                        "public_name": grant.public_name,
                    }
                raise
            self._owns(actor, grant, snapshot)
            session = snapshot.session
            if session.state in {"active", "stopping"}:
                session = await self.repository.begin_managed_stop(
                    grant.logical_agent_id,
                    session.work_session_id,
                    session.session_epoch,
                    principal_id=grant.principal_id,
                    reason=reason,
                    now=self.clock(),
                )
                if not await self._drain(session, session.end_reason or reason):
                    return {
                        "ok": True,
                        "session_state": "stopping",
                        "cleanup_pending": True,
                        "public_name": grant.public_name,
                    }
                session = await self.repository.finish_managed_stop(
                    grant.logical_agent_id,
                    session.work_session_id,
                    session.session_epoch,
                    principal_id=grant.principal_id,
                    now=self.clock(),
                )
            return {
                "ok": True,
                "session_state": "inactive",
                "public_name": grant.public_name,
                "work_session_id": session.work_session_id,
            }

    async def has_managed_window(self, logical_agent_id: str) -> bool:
        return await self.repository.current_window(logical_agent_id) is not None

    async def operator_status(self, actor: ActorContext, logical_agent_id: str) -> dict:
        grant = await self._authorize(actor, ManagedOperation.OPERATOR_READ, logical_agent_id)
        with actor.bind():
            now = self.clock()
            policy = await self.repository.policy(logical_agent_id)
            window = await self.repository.current_window(logical_agent_id)
            session = await self.repository.active_session_for_slot(logical_agent_id)
            window_payload = None
            if window is not None:
                phase = window.phase(now)
                remaining = window.remaining_seconds(now)
                window_payload = {
                    "work_window_id": window.work_window_id,
                    "state": phase.value,
                    "lifecycle": window.lifecycle.value,
                    "opened_at": window.opened_at.isoformat(),
                    "elapsed_seconds": max(0, window.effective_duration_seconds - remaining),
                    "remaining_seconds": remaining,
                    "initial_duration_seconds": window.initial_duration_seconds,
                    "effective_duration_seconds": window.effective_duration_seconds,
                    "hard_expires_at": window.hard_expires_at.isoformat(),
                    "warning_before_expiry_seconds": window.warning_before_expiry_seconds,
                    "draining_before_expiry_seconds": window.draining_before_expiry_seconds,
                    "rearm_after_seconds": window.rearm_after_seconds,
                    "rearm_at": (
                        window.rearm_at.isoformat() if window.rearm_at is not None else None
                    ),
                    "window_revision": window.window_revision,
                }
            session_payload = None
            if session is not None:
                snapshot = await self.repository.session_snapshot(
                    logical_agent_id, session.work_session_id, session.session_epoch
                )
                if (
                    snapshot.session.authority_node_id != grant.authority_node_id
                    or snapshot.session.authority_epoch != grant.authority_epoch
                ):
                    raise ManagedSessionError("session_authority_stale", return_to_chat=True)
                session_payload = {
                    "work_session_id": snapshot.session.work_session_id,
                    "session_epoch": snapshot.session.session_epoch,
                    "role": snapshot.binding.role,
                    "contract_version": snapshot.binding.contract_version,
                    "state": snapshot.session.state,
                    "started_at": snapshot.session.started_at,
                    "ended_at": snapshot.session.ended_at,
                    "end_reason": snapshot.session.end_reason,
                }
            return {
                "ok": True,
                "logical_agent_id": logical_agent_id,
                "public_name": grant.public_name,
                "authority_node_id": grant.authority_node_id,
                "authority_epoch": grant.authority_epoch,
                "policy": {
                    "default_duration_seconds": policy.policy.default_duration_seconds,
                    "warning_before_expiry_seconds": policy.policy.warning_before_expiry_seconds,
                    "draining_before_expiry_seconds": policy.policy.draining_before_expiry_seconds,
                    "rearm_after_seconds": policy.policy.rearm_after_seconds,
                    "revision": policy.revision,
                },
                "window": window_payload,
                "session": session_payload,
            }

    async def operator_end(self, actor: ActorContext, logical_agent_id: str) -> dict:
        grant = await self._authorize(actor, ManagedOperation.OPERATOR_END, logical_agent_id)
        with actor.bind():
            session = await self.repository.active_session_for_slot(logical_agent_id)
            if session is None:
                return {
                    "ok": True,
                    "session_state": "inactive",
                    "logical_agent_id": logical_agent_id,
                    "public_name": grant.public_name,
                }
            snapshot = await self.repository.session_snapshot(
                logical_agent_id, session.work_session_id, session.session_epoch
            )
            if (
                snapshot.session.authority_node_id != grant.authority_node_id
                or snapshot.session.authority_epoch != grant.authority_epoch
            ):
                raise ManagedSessionError("session_authority_stale", return_to_chat=True)
            session = snapshot.session
            if session.state in {"active", "stopping"}:
                session = await self.repository.begin_managed_stop(
                    logical_agent_id,
                    session.work_session_id,
                    session.session_epoch,
                    principal_id=grant.principal_id,
                    reason="operator_end",
                    now=self.clock(),
                )
                if not await self._drain(session, "operator_end"):
                    return {
                        "ok": True,
                        "session_state": "stopping",
                        "cleanup_pending": True,
                        "logical_agent_id": logical_agent_id,
                        "public_name": grant.public_name,
                        "work_session_id": session.work_session_id,
                    }
                session = await self.repository.finish_managed_stop(
                    logical_agent_id,
                    session.work_session_id,
                    session.session_epoch,
                    principal_id=grant.principal_id,
                    now=self.clock(),
                )
            return {
                "ok": True,
                "session_state": "inactive",
                "logical_agent_id": logical_agent_id,
                "public_name": grant.public_name,
                "work_session_id": session.work_session_id,
            }

    async def change_policy(
        self,
        actor: ActorContext,
        logical_agent_id: str,
        policy: SlotSessionPolicy,
        *,
        expected_revision: int,
    ) -> SlotPolicyRecord:
        grant = await self._authorize(actor, ManagedOperation.OPERATOR_POLICY, logical_agent_id)
        with actor.bind():
            return await self.repository.update_policy(
                logical_agent_id,
                policy,
                expected_revision=expected_revision,
                principal_id=grant.principal_id,
                now=self.clock(),
            )

    async def change_window(
        self,
        actor: ActorContext,
        logical_agent_id: str,
        *,
        expected_revision: int,
        duration_seconds: int | None = None,
        delta_seconds: int | None = None,
        warning_before_expiry_seconds: int | None = None,
        draining_before_expiry_seconds: int | None = None,
    ) -> ManagedWindowMutation:
        grant = await self._authorize(actor, ManagedOperation.OPERATOR_WINDOW, logical_agent_id)
        with actor.bind():
            change = await self.repository.change_window(
                logical_agent_id,
                expected_revision=expected_revision,
                principal_id=grant.principal_id,
                now=self.clock(),
                duration_seconds=duration_seconds,
                delta_seconds=delta_seconds,
                warning_before_expiry_seconds=warning_before_expiry_seconds,
                draining_before_expiry_seconds=draining_before_expiry_seconds,
            )
            pending = False
            if change.current.lifecycle is WindowLifecycle.EXPIRED:
                pending = not await self._safe_reconcile(change.current)
            # The mutation has committed even if OS/Fleet cleanup is pending.
            # Never misreport it as an uncommitted/retryable mutation failure.
            return ManagedWindowMutation(change, cleanup_pending=pending)

    async def _safe_reconcile(self, window: WorkWindow) -> bool:
        try:
            return await self.reconcile_window(window)
        except Exception as exc:
            # Reconciliation is retryable; revocation is NOT rolled back. Never
            # hide task cancellation or process shutdown behind this wrapper.
            _LOG.warning("managed window reconciliation deferred: %s", type(exc).__name__)
            return False

    async def reconcile_window(self, window: WorkWindow) -> bool:
        """Bounded internal recovery: revoke first, drain, then durable cooldown."""
        now = self.clock()
        # Re-read the authoritative current revision; an old timer must never
        # expire a window that has since been extended or superseded.
        latest = await self.repository.current_window(window.logical_agent_id)
        if latest is None or latest.work_window_id != window.work_window_id:
            return True
        window = latest
        if window.lifecycle is WindowLifecycle.OPEN and now >= window.hard_expires_at:
            window = await self.repository.expire_window(
                window.logical_agent_id,
                expected_revision=window.window_revision,
                now=now,
            )
        if window.lifecycle is WindowLifecycle.COOLDOWN:
            return True
        session = await self.repository.active_session_for_slot(window.logical_agent_id)
        if session is not None:
            exact = await self.repository.session_snapshot(
                window.logical_agent_id, session.work_session_id, session.session_epoch
            )
            if exact.binding.work_window_id != window.work_window_id:
                raise ManagedSessionError("session_binding_invalid")
            if window.lifecycle is WindowLifecycle.OPEN and session.state == "active":
                return True
            if not await self._drain(session, session.end_reason or "hard_duration"):
                return False
            await self.repository.finish_managed_stop(
                window.logical_agent_id,
                session.work_session_id,
                session.session_epoch,
                principal_id="system",
                now=now,
            )
        if window.lifecycle is WindowLifecycle.EXPIRED:
            await self.repository.complete_window_fence(
                window.logical_agent_id, expected_revision=window.window_revision, now=now
            )
        return True

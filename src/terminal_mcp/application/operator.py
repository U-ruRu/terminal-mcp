"""Operator capability for slots, policy and explicit legacy session control.

The application owns policy/availability decisions. HTTP models are not imported
here, and an actor is always supplied by a trusted inbound adapter. Existing
persistent domain services retain their transactional and idempotency guarantees.
"""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedSessionError
from terminal_mcp.core.persistent_policy import PersistentPolicyError
from terminal_mcp.core.work_windows import SlotSessionPolicy, WorkWindowError


class OperatorApplication:
    def __init__(self, service, policy_controller=None, managed_sessions=None):
        self._service = service
        self.policy_controller = policy_controller
        self.managed_sessions = managed_sessions

    def _backend(self):
        return getattr(self._service, "persistent", None)

    @staticmethod
    def _unavailable():
        return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}

    @staticmethod
    def _require_operator(actor: ActorContext) -> None:
        if actor.endpoint_role != "operator":
            raise PermissionError("operator_role_required")

    async def policy_update(
        self,
        actor: ActorContext,
        *,
        duration_seconds: int | None,
        warning_after_seconds: int | None,
        alert_after_seconds: int | None,
        rearm_after_seconds: int | None,
        legacy_admission_enabled: bool | None,
    ):
        self._require_operator(actor)
        with actor.bind():
            if self.policy_controller is None:
                return self._unavailable()
            try:
                policy = await self.policy_controller.update(
                    duration_seconds=duration_seconds,
                    warning_after_seconds=warning_after_seconds,
                    alert_after_seconds=alert_after_seconds,
                    rearm_after_seconds=rearm_after_seconds,
                    legacy_admission_enabled=legacy_admission_enabled,
                )
            except PersistentPolicyError as exc:
                result = {"ok": False, "code": exc.code, "error": exc.code}
                if exc.blockers:
                    result["blockers"] = exc.blockers
                return result
            return {"ok": True, "policy": policy}

    async def slot_list(self, actor: ActorContext):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            return await target.slot_list() if target else self._unavailable()

    async def slot_get(self, actor: ActorContext, *, logical_agent_id: str):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            return await target.slot_get(logical_agent_id) if target else self._unavailable()

    async def slot_create(self, actor: ActorContext, *, display_name: str):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            return await target.slot_create(display_name) if target else self._unavailable()

    async def slot_rename(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
        display_name: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.slot_rename(
                logical_agent_id,
                display_name,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    async def slot_rotate(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
        selector: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.slot_rotate_selector(
                logical_agent_id,
                selector,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    async def slot_migrate_access(self, actor: ActorContext, *, logical_agent_id: str):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            return (
                await target.slot_migrate_access(logical_agent_id)
                if target
                else self._unavailable()
            )

    async def slot_rotate_access_code(self, actor: ActorContext, *, logical_agent_id: str):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            return (
                await target.slot_rotate_access_code(logical_agent_id)
                if target
                else self._unavailable()
            )

    async def slot_play(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.slot_play(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    async def slot_suspend(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.slot_suspend(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    async def slot_delete(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.slot_delete(
                logical_agent_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    async def session_start(self, actor: ActorContext, *, selector: str, expected_revision: int):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.session_start(selector, expected_revision=expected_revision)

    async def session_end(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.session_end(logical_agent_id, work_session_id, session_epoch)

    async def run(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        cmd: str,
        queue_id: int | None,
        task_scope: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.run(
                cmd,
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                queue_id=queue_id,
                task_scope=task_scope,
            )

    async def cancel(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        cmd_hash: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.cancel(
                cmd_hash,
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
            )

    async def task(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        action: str,
        namespace: str,
        task_id: str | None,
        payload: dict[str, object],
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.task(
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                action=action,
                namespace=namespace,
                task_id=task_id,
                **payload,
            )

    async def claim_release(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        namespace: str,
        task_id: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.claim_release(
                namespace,
                task_id,
                logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
            )

    async def claim_reassign(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        namespace: str,
        task_id: str,
        to_logical_agent_id: str,
        expected_revision: int,
        idempotency_key: str,
    ):
        self._require_operator(actor)
        with actor.bind():
            target = self._backend()
            if not target:
                return self._unavailable()
            return await target.claim_reassign(
                namespace,
                task_id,
                logical_agent_id,
                to_logical_agent_id,
                work_session_id=work_session_id,
                session_epoch=session_epoch,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _managed_error(exc: Exception) -> dict:
        code = getattr(exc, "code", "managed_session_error")
        result = {"ok": False, "code": code, "error": code}
        if getattr(exc, "return_to_chat", False):
            result["return_to_chat"] = True
        return result

    async def managed_status(self, actor: ActorContext, *, logical_agent_id: str):
        self._require_operator(actor)
        if self.managed_sessions is None:
            return self._unavailable()
        try:
            return await self.managed_sessions.operator_status(actor, logical_agent_id)
        except (ManagedSessionError, WorkWindowError) as exc:
            return self._managed_error(exc)

    async def managed_policy_update(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        default_duration_seconds: int,
        warning_before_expiry_seconds: int,
        draining_before_expiry_seconds: int,
        rearm_after_seconds: int,
    ):
        self._require_operator(actor)
        if self.managed_sessions is None:
            return self._unavailable()
        try:
            policy = SlotSessionPolicy(
                default_duration_seconds=default_duration_seconds,
                warning_before_expiry_seconds=warning_before_expiry_seconds,
                draining_before_expiry_seconds=draining_before_expiry_seconds,
                rearm_after_seconds=rearm_after_seconds,
            )
            record = await self.managed_sessions.change_policy(
                actor, logical_agent_id, policy, expected_revision=expected_revision
            )
        except (ManagedSessionError, WorkWindowError) as exc:
            return self._managed_error(exc)
        return {
            "ok": True,
            "logical_agent_id": logical_agent_id,
            "policy": {
                "default_duration_seconds": record.policy.default_duration_seconds,
                "warning_before_expiry_seconds": record.policy.warning_before_expiry_seconds,
                "draining_before_expiry_seconds": record.policy.draining_before_expiry_seconds,
                "rearm_after_seconds": record.policy.rearm_after_seconds,
                "revision": record.revision,
            },
        }

    async def managed_window_update(
        self,
        actor: ActorContext,
        *,
        logical_agent_id: str,
        expected_revision: int,
        duration_seconds: int | None = None,
        delta_seconds: int | None = None,
        warning_before_expiry_seconds: int | None = None,
        draining_before_expiry_seconds: int | None = None,
    ):
        self._require_operator(actor)
        if self.managed_sessions is None:
            return self._unavailable()
        try:
            mutation = await self.managed_sessions.change_window(
                actor,
                logical_agent_id,
                expected_revision=expected_revision,
                duration_seconds=duration_seconds,
                delta_seconds=delta_seconds,
                warning_before_expiry_seconds=warning_before_expiry_seconds,
                draining_before_expiry_seconds=draining_before_expiry_seconds,
            )
        except (ManagedSessionError, WorkWindowError) as exc:
            return self._managed_error(exc)
        change = mutation.change
        current = change.current
        return {
            "ok": True,
            "logical_agent_id": logical_agent_id,
            "cleanup_pending": mutation.cleanup_pending,
            "previous": {
                "effective_duration_seconds": change.previous.effective_duration_seconds,
                "hard_expires_at": change.previous.hard_expires_at.isoformat(),
                "warning_before_expiry_seconds": change.previous.warning_before_expiry_seconds,
                "draining_before_expiry_seconds": change.previous.draining_before_expiry_seconds,
                "window_revision": change.previous.window_revision,
            },
            "current": {
                "effective_duration_seconds": current.effective_duration_seconds,
                "hard_expires_at": current.hard_expires_at.isoformat(),
                "warning_before_expiry_seconds": current.warning_before_expiry_seconds,
                "draining_before_expiry_seconds": current.draining_before_expiry_seconds,
                "window_revision": current.window_revision,
                "state": change.state.value,
            },
        }

    async def managed_session_end(self, actor: ActorContext, *, logical_agent_id: str):
        self._require_operator(actor)
        if self.managed_sessions is None:
            return self._unavailable()
        try:
            return await self.managed_sessions.operator_end(actor, logical_agent_id)
        except (ManagedSessionError, WorkWindowError) as exc:
            return self._managed_error(exc)

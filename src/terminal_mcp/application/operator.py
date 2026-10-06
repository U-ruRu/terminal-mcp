"""Operator capability for slots, policy and explicit legacy session control.

The application owns policy/availability decisions. HTTP models are not imported
here, and an actor is always supplied by a trusted inbound adapter. Existing
persistent domain services retain their transactional and idempotency guarantees.
"""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_policy import PersistentPolicyError


class OperatorApplication:
    def __init__(self, service, policy_controller=None):
        self._service = service
        self.policy_controller = policy_controller

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

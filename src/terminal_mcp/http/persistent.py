from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.core.persistent_policy import PersistentPolicyError


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersistentPolicyRequest(StrictRequest):
    duration_seconds: int | None = Field(default=None, ge=1)
    warning_after_seconds: int | None = Field(default=None, ge=1)
    alert_after_seconds: int | None = Field(default=None, ge=1)
    rearm_after_seconds: int | None = Field(default=None, ge=1)
    legacy_admission_enabled: bool | None = None

    @model_validator(mode="after")
    def require_one_change(self):
        if (
            self.duration_seconds is None
            and self.warning_after_seconds is None
            and self.alert_after_seconds is None
            and self.rearm_after_seconds is None
            and self.legacy_admission_enabled is None
        ):
            raise ValueError("at least one policy field is required")
        return self


class SlotCreateRequest(StrictRequest):
    display_name: str = Field(max_length=120)


class SlotGetRequest(StrictRequest):
    logical_agent_id: str = Field(min_length=1, max_length=128)


class SlotMutationRequest(SlotGetRequest):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class SlotRenameRequest(SlotMutationRequest):
    display_name: str = Field(max_length=120)


class SlotRotateRequest(SlotMutationRequest):
    selector: str = Field(min_length=4, max_length=4)


class SessionStartRequest(StrictRequest):
    selector: str = Field(min_length=4, max_length=4)
    expected_revision: int = Field(ge=1)


class SessionRequest(StrictRequest):
    logical_agent_id: str = Field(min_length=1, max_length=128)
    work_session_id: str = Field(min_length=1, max_length=128)
    session_epoch: int = Field(ge=1)


class PersistentRunRequest(SessionRequest):
    cmd: str = Field(min_length=1)
    queue_id: int | None = Field(default=None, ge=1)
    task_scope: str = Field(min_length=1, max_length=260)


class PersistentCancelRequest(SessionRequest):
    cmd_hash: str = Field(min_length=1, max_length=128)


class PersistentTaskRequest(SessionRequest):
    action: str = Field(min_length=1, max_length=32)
    namespace: str = Field(min_length=1, max_length=120)
    task_id: str | None = Field(default=None, max_length=120)
    payload: dict[str, object] = Field(default_factory=dict)


class ClaimReleaseRequest(SessionRequest):
    namespace: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)


class ClaimReassignRequest(ClaimReleaseRequest):
    to_logical_agent_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


def build_persistent_router(service, policy_controller=None) -> APIRouter:
    router = APIRouter(prefix="/actions/persistent", tags=["persistent-agents"])

    def backend():
        return getattr(service, "persistent", None)

    def unavailable():
        return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}

    @router.post("/policy", operation_id="updatePersistentPolicy")
    async def policy_update(body: PersistentPolicyRequest):
        if policy_controller is None:
            return unavailable()
        try:
            policy = await policy_controller.update(
                duration_seconds=body.duration_seconds,
                warning_after_seconds=body.warning_after_seconds,
                alert_after_seconds=body.alert_after_seconds,
                rearm_after_seconds=body.rearm_after_seconds,
                legacy_admission_enabled=body.legacy_admission_enabled,
            )
        except PersistentPolicyError as exc:
            result = {"ok": False, "code": exc.code, "error": exc.code}
            if exc.blockers:
                result["blockers"] = exc.blockers
            return result
        return {"ok": True, "policy": policy}

    @router.post("/slots/list", operation_id="listPersistentSlots")
    async def slot_list():
        target = backend()
        return await target.slot_list() if target else unavailable()

    @router.post("/slots/get", operation_id="getPersistentSlot")
    async def slot_get(body: SlotGetRequest):
        target = backend()
        return await target.slot_get(body.logical_agent_id) if target else unavailable()

    @router.post("/slots/create", operation_id="createPersistentSlot")
    async def slot_create(body: SlotCreateRequest):
        target = backend()
        return await target.slot_create(body.display_name) if target else unavailable()

    @router.post("/slots/rename", operation_id="renamePersistentSlot")
    async def slot_rename(body: SlotRenameRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.slot_rename(
            body.logical_agent_id,
            body.display_name,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @router.post("/slots/rotate-selector", operation_id="rotatePersistentSlotSelector")
    async def slot_rotate(body: SlotRotateRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.slot_rotate_selector(
            body.logical_agent_id,
            body.selector,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @router.post("/slots/migrate-access", operation_id="migratePersistentSlotAccess")
    async def slot_migrate_access(body: SlotGetRequest):
        target = backend()
        return await target.slot_migrate_access(body.logical_agent_id) if target else unavailable()

    @router.post("/slots/rotate-access-code", operation_id="rotatePersistentSlotAccessCode")
    async def slot_rotate_access_code(body: SlotGetRequest):
        target = backend()
        return (
            await target.slot_rotate_access_code(body.logical_agent_id) if target else unavailable()
        )

    @router.post("/slots/play", operation_id="playPersistentSlot")
    async def slot_play(body: SlotMutationRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.slot_play(
            body.logical_agent_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @router.post("/slots/suspend", operation_id="suspendPersistentSlot")
    async def slot_suspend(body: SlotMutationRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.slot_suspend(
            body.logical_agent_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @router.post("/slots/delete", operation_id="deletePersistentSlot")
    async def slot_delete(body: SlotMutationRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.slot_delete(
            body.logical_agent_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @router.post("/sessions/start", operation_id="startPersistentSession")
    async def session_start(body: SessionStartRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.session_start(body.selector, expected_revision=body.expected_revision)

    @router.post("/sessions/end", operation_id="endPersistentSession")
    async def session_end(body: SessionRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.session_end(
            body.logical_agent_id, body.work_session_id, body.session_epoch
        )

    @router.post("/run", operation_id="runPersistentCommand")
    async def run(body: PersistentRunRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.run(
            body.cmd,
            logical_agent_id=body.logical_agent_id,
            work_session_id=body.work_session_id,
            session_epoch=body.session_epoch,
            queue_id=body.queue_id,
            task_scope=body.task_scope,
        )

    @router.post("/cancel", operation_id="cancelPersistentCommand")
    async def cancel(body: PersistentCancelRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.cancel(
            body.cmd_hash,
            logical_agent_id=body.logical_agent_id,
            work_session_id=body.work_session_id,
            session_epoch=body.session_epoch,
        )

    @router.post("/task", operation_id="mutatePersistentTask")
    async def task(body: PersistentTaskRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.task(
            logical_agent_id=body.logical_agent_id,
            work_session_id=body.work_session_id,
            session_epoch=body.session_epoch,
            action=body.action,
            namespace=body.namespace,
            task_id=body.task_id,
            **body.payload,
        )

    @router.post("/claims/release", operation_id="releasePersistentClaim")
    async def claim_release(body: ClaimReleaseRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.claim_release(
            body.namespace,
            body.task_id,
            body.logical_agent_id,
            work_session_id=body.work_session_id,
            session_epoch=body.session_epoch,
        )

    @router.post("/claims/reassign", operation_id="reassignPersistentClaim")
    async def claim_reassign(body: ClaimReassignRequest):
        target = backend()
        if not target:
            return unavailable()
        return await target.claim_reassign(
            body.namespace,
            body.task_id,
            body.logical_agent_id,
            body.to_logical_agent_id,
            work_session_id=body.work_session_id,
            session_epoch=body.session_epoch,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    return router

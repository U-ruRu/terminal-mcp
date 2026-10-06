from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.application import get_application


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
    target = get_application(service).operator
    if policy_controller is not None:
        target.policy_controller = policy_controller

    @router.post("/policy", operation_id="updatePersistentPolicy")
    async def policy_update(body: PersistentPolicyRequest):
        return await target.policy_update(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/list", operation_id="listPersistentSlots")
    async def slot_list():
        return await target.slot_list(
            actor_for(service, transport="http", endpoint_role="operator")
        )

    @router.post("/slots/get", operation_id="getPersistentSlot")
    async def slot_get(body: SlotGetRequest):
        return await target.slot_get(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/create", operation_id="createPersistentSlot")
    async def slot_create(body: SlotCreateRequest):
        return await target.slot_create(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/rename", operation_id="renamePersistentSlot")
    async def slot_rename(body: SlotRenameRequest):
        return await target.slot_rename(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/rotate-selector", operation_id="rotatePersistentSlotSelector")
    async def slot_rotate(body: SlotRotateRequest):
        return await target.slot_rotate(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/migrate-access", operation_id="migratePersistentSlotAccess")
    async def slot_migrate_access(body: SlotGetRequest):
        return await target.slot_migrate_access(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/rotate-access-code", operation_id="rotatePersistentSlotAccessCode")
    async def slot_rotate_access_code(body: SlotGetRequest):
        return await target.slot_rotate_access_code(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/play", operation_id="playPersistentSlot")
    async def slot_play(body: SlotMutationRequest):
        return await target.slot_play(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/suspend", operation_id="suspendPersistentSlot")
    async def slot_suspend(body: SlotMutationRequest):
        return await target.slot_suspend(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/slots/delete", operation_id="deletePersistentSlot")
    async def slot_delete(body: SlotMutationRequest):
        return await target.slot_delete(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/sessions/start", operation_id="startPersistentSession")
    async def session_start(body: SessionStartRequest):
        return await target.session_start(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/sessions/end", operation_id="endPersistentSession")
    async def session_end(body: SessionRequest):
        return await target.session_end(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/run", operation_id="runPersistentCommand")
    async def run(body: PersistentRunRequest):
        return await target.run(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/cancel", operation_id="cancelPersistentCommand")
    async def cancel(body: PersistentCancelRequest):
        return await target.cancel(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/task", operation_id="mutatePersistentTask")
    async def task(body: PersistentTaskRequest):
        return await target.task(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/claims/release", operation_id="releasePersistentClaim")
    async def claim_release(body: ClaimReleaseRequest):
        return await target.claim_release(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    @router.post("/claims/reassign", operation_id="reassignPersistentClaim")
    async def claim_reassign(body: ClaimReassignRequest):
        return await target.claim_reassign(
            actor_for(service, transport="http", endpoint_role="operator"), **body.model_dump()
        )

    return router

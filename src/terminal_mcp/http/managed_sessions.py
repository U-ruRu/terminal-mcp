from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.application import get_application


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagedSlotRequest(StrictRequest):
    logical_agent_id: str = Field(min_length=1, max_length=128)


class ManagedPolicyRequest(ManagedSlotRequest):
    expected_revision: int = Field(ge=1)
    default_duration_seconds: int = Field(ge=1)
    warning_before_expiry_seconds: int = Field(ge=0)
    draining_before_expiry_seconds: int = Field(ge=0)
    rearm_after_seconds: int = Field(ge=0)


class ManagedWindowRequest(ManagedSlotRequest):
    expected_revision: int = Field(ge=1)
    duration_seconds: int | None = Field(default=None, ge=1)
    delta_seconds: int | None = None
    warning_before_expiry_seconds: int | None = Field(default=None, ge=0)
    draining_before_expiry_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_one_change(self):
        if self.duration_seconds is not None and self.delta_seconds is not None:
            raise ValueError("duration_seconds and delta_seconds are mutually exclusive")
        if all(
            value is None
            for value in (
                self.duration_seconds,
                self.delta_seconds,
                self.warning_before_expiry_seconds,
                self.draining_before_expiry_seconds,
            )
        ):
            raise ValueError("at least one window field is required")
        return self


def build_managed_sessions_router(service) -> APIRouter:
    router = APIRouter(prefix="/actions/persistent/managed", tags=["managed-work-sessions"])
    target = get_application(service).operator

    def operator_actor():
        return actor_for(service, transport="http", endpoint_role="operator")

    @router.post("/status", operation_id="getManagedWorkSessionStatus")
    async def managed_status(body: ManagedSlotRequest):
        return await target.managed_status(operator_actor(), **body.model_dump())

    @router.post("/policy", operation_id="updateManagedSlotSessionPolicy")
    async def managed_policy_update(body: ManagedPolicyRequest):
        return await target.managed_policy_update(operator_actor(), **body.model_dump())

    @router.post("/window", operation_id="updateManagedWorkWindow")
    async def managed_window_update(body: ManagedWindowRequest):
        return await target.managed_window_update(operator_actor(), **body.model_dump())

    @router.post("/sessions/end", operation_id="endManagedWorkSession")
    async def managed_session_end(body: ManagedSlotRequest):
        return await target.managed_session_end(operator_actor(), **body.model_dump())

    return router

"""Mobile/Console Access Mesh controls. No command execution on the operator API."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.application.access_mesh_operator import AccessMeshOperator
from terminal_mcp.core.public_errors import error_from_exception, public_error


class PolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    duration_seconds: int | None = Field(default=None, ge=1)
    cooldown_seconds: int | None = Field(default=None, ge=0)
    rearm_enabled: bool | None = None
    warning_seconds: int | None = Field(default=None, ge=0)
    draining_seconds: int | None = Field(default=None, ge=0)
    release_on_end: bool | None = None

    @model_validator(mode="after")
    def no_null_policy(self):
        if any(
            getattr(self, name) is None and name != "release_on_end"
            for name in self.model_fields_set
        ):
            raise ValueError("policy values must have concrete types")
        return self


class OperatorMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "create", "defaults", "policy", "suspend", "resume", "delete", "rotate", "deadline", "end"
    ]
    idempotency_key: str = Field(min_length=1, max_length=128)
    slot_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)
    mode: Literal["legacy", "persistent"] | None = None
    policy: PolicyPatch | None = None
    deadline_at: str | None = Field(default=None, min_length=1, max_length=64)
    legacy_enabled: bool | None = None

    @model_validator(mode="after")
    def selected_fields(self):
        extra = self.model_fields_set - {"action", "idempotency_key"}
        allowed = (
            {"mode", "policy"}
            if self.action == "create"
            else {"expected_revision", "policy", "legacy_enabled"}
            if self.action == "defaults"
            else {"slot_id", "expected_revision"}
            | (
                {"policy"}
                if self.action == "policy"
                else {"deadline_at"}
                if self.action == "deadline"
                else set()
            )
        )
        if extra - allowed:
            raise ValueError("fields do not belong to the selected action")
        required = (
            {"mode"}
            if self.action == "create"
            else {"expected_revision"}
            if self.action == "defaults"
            else {"slot_id", "expected_revision"}
            | (
                {"policy"}
                if self.action == "policy"
                else {"deadline_at"}
                if self.action == "deadline"
                else set()
            )
        )
        if any(getattr(self, key) is None for key in required):
            raise ValueError("selected action requires its fields")
        return self


def build_access_mesh_operator_router(application):
    router = APIRouter(prefix="/actions/access", tags=["access-mesh-operator"])
    operator = AccessMeshOperator(application.service.access_mesh)

    def actor():
        return actor_for(application, transport="http", endpoint_role="operator")

    async def invoke(fn, **kwargs):
        try:
            who = actor()
            with who.bind():
                return await fn(who, **kwargs)
        except Exception as exc:
            return error_from_exception(exc).as_dict()

    @router.get("/slots", operation_id="listAccessMeshSlots")
    async def slots(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return await invoke(operator.slots, limit=limit, cursor=cursor)

    @router.get("/slots/{slot_id}", operation_id="getAccessMeshSlot")
    async def get(slot_id: str):
        return await invoke(operator.get, slot_id=slot_id)

    @router.get("/defaults", operation_id="getAccessMeshDefaults")
    async def defaults():
        return await invoke(operator.defaults)

    @router.post(
        "/mutate",
        operation_id="mutateAccessMesh",
        openapi_extra={
            "x-runtime-schema": OperatorMutation.model_json_schema(),
            "x-readOnly": False,
            "x-destructive": True,
            "x-idempotent": True,
            "x-action-matrix": {
                action: {
                    "readOnly": False,
                    "destructive": action in {"delete", "end", "suspend"},
                    "idempotent": True,
                }
                for action in OperatorMutation.model_fields["action"].annotation.__args__
            },
        },
    )
    async def mutate(payload: Annotated[object, Body()]):
        try:
            request = OperatorMutation.model_validate(payload)
        except ValidationError as exc:
            return error_from_exception(exc).as_dict()
        except Exception:
            return public_error("invalid_request").as_dict()
        values = request.model_dump()
        values["policy"] = request.policy.model_dump(exclude_unset=True) if request.policy else None
        return await invoke(operator.mutate, **values)

    return router

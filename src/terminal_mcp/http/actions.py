# ruff: noqa: E501
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.api_models import (
    AgentFinishResponse,
    AgentOverviewResponse,
    CancelResponse,
    CoordinateResponse,
    HealthResponse,
    MessageResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
)
from terminal_mcp.core.orchestration import public_agent_name
from terminal_mcp.core.service import DEFAULT_READ_LINES, MAX_READ_LINES
from terminal_mcp.telemetry import observed

ScopeItem = Annotated[str, Field(min_length=1, max_length=80)]
StepItem = Annotated[str, Field(min_length=1, max_length=160)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRequest(StrictRequest):
    agent_id: str = Field(min_length=1, max_length=64)


class OptionalAgentRequest(StrictRequest):
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentStartRequest(StrictRequest):
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_summary: str | None = Field(default=None, min_length=1, max_length=120)
    intent: str | None = Field(default=None, min_length=1, max_length=160)
    details: list[StepItem] | None = Field(default=None, min_length=1, max_length=12)
    work_scope: list[ScopeItem] | None = Field(default=None, max_length=4)

    @model_validator(mode="after")
    def validate_start(self):
        if self.agent_id is None:
            if self.task_summary is None or self.intent is None or self.details is None:
                raise ValueError(
                    "new registration requires task_summary, intent and details"
                )
        elif all(
            value is None
            for value in (self.task_summary, self.intent, self.details, self.work_scope)
        ):
            raise ValueError("agent_start update requires at least one field to change")
        return self


class CoordinateRequest(AgentRequest):
    step: int | None = Field(default=None, ge=1)
    intent: str | None = Field(default=None, min_length=1, max_length=160)
    show_details: bool = False

    @model_validator(mode="after")
    def validate_coordinate(self):
        if self.intent is not None and self.step is None:
            raise ValueError("step is required when intent is provided")
        return self


class MessageRequest(AgentRequest):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    target: str | None = Field(default=None, min_length=1, max_length=64)
    message_hash: str | None = Field(default=None, min_length=8, max_length=8)
    require_reply: bool = False
    alert: bool = False

    @model_validator(mode="after")
    def validate_message(self):
        if self.message_hash is not None:
            if self.target is not None or self.require_reply or self.alert:
                raise ValueError("acknowledgement/reply inherits target and message policy")
        elif self.text is None:
            raise ValueError("sending requires text")
        return self


class AgentsRequest(OptionalAgentRequest):
    target: str | None = Field(default=None, min_length=1, max_length=64)
    show_details: bool = False
    show_intents: bool = False
    show_commands: bool = False
    command_hash: str | None = Field(default=None, min_length=8, max_length=8)
    since_minutes: int | None = Field(default=None, ge=1, le=10080)


class RunRequest(AgentRequest):
    cmd: str = Field(min_length=1, description="Shell script passed to /bin/bash -s through stdin.")
    queue_id: int | None = Field(default=None, ge=1)


class ReadRequest(OptionalAgentRequest):
    cmd_hash: str | None = Field(default=None, min_length=8, max_length=8)
    lines_count: int = Field(default=DEFAULT_READ_LINES, ge=1, le=MAX_READ_LINES)
    offset: int | None = None


class RecoveryRequest(OptionalAgentRequest):
    cmd: str = Field(min_length=1)


class CancelRequest(OptionalAgentRequest):
    cmd_hash: str = Field(min_length=8, max_length=8)


def build_actions_router(service, auth_mode="none"):
    router = APIRouter(prefix="/actions", tags=["terminal-actions"])

    @router.post(
        "/agent/start",
        operation_id="startAgentSession",
        response_model=AgentOverviewResponse,
        response_model_exclude_none=True,
    )
    async def agent_start(body: AgentStartRequest):
        return await observed(
            service,
            "rest",
            "agent_start",
            service.agent_start(
                task_summary=body.task_summary,
                intent=body.intent,
                details=body.details,
                work_scope=body.work_scope,
                agent_id=body.agent_id,
            ),
        )

    @router.post(
        "/coordinate",
        operation_id="coordinateAgent",
        response_model=CoordinateResponse,
        response_model_exclude_none=True,
    )
    async def coordinate(body: CoordinateRequest):
        return await observed(
            service,
            "rest",
            "coordinate",
            service.coordinate(
                body.agent_id,
                step=body.step,
                intent=body.intent,
                show_details=body.show_details,
            ),
        )

    @router.post(
        "/message",
        operation_id="messageAgent",
        response_model=MessageResponse,
        response_model_exclude_none=True,
    )
    async def message(body: MessageRequest):
        return await observed(
            service,
            "rest",
            "message",
            service.message(
                body.agent_id,
                text=body.text,
                target=body.target,
                message_hash=body.message_hash,
                require_reply=body.require_reply,
                alert=body.alert,
            ),
        )

    @router.post(
        "/agents",
        operation_id="listActiveAgents",
        response_model=AgentOverviewResponse,
        response_model_exclude_none=True,
    )
    async def agents(body: AgentsRequest):
        return await observed(
            service,
            "rest",
            "agents",
            service.agents(
                body.agent_id,
                target=body.target,
                show_details=body.show_details,
                show_intents=body.show_intents,
                show_commands=body.show_commands,
                command_hash=body.command_hash,
                since_minutes=body.since_minutes,
            ),
        )

    @router.post(
        "/agent/finish",
        operation_id="finishAgentSession",
        response_model=AgentFinishResponse,
        response_model_exclude_none=True,
    )
    async def agent_finish(body: AgentRequest):
        return await observed(service, "rest", "agent_finish", service.agent_finish(body.agent_id))

    @router.post("/run", operation_id="runCommand", response_model=RunResponse)
    async def run_command(body: RunRequest):
        return await observed(
            service, "rest", "run",
            service.run(body.cmd, agent_id=body.agent_id, queue_id=body.queue_id)
        )

    @router.post("/recovery", operation_id="recoveryCommand", response_model=RecoveryResponse)
    async def recovery_command(body: RecoveryRequest):
        result = await observed(
            service, "rest", "recovery", service.recovery(body.cmd, agent_id=body.agent_id)
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(body.agent_id)
        return result

    @router.post("/read", operation_id="readTerminal", response_model=ReadResponse)
    async def read_terminal(body: ReadRequest):
        result = await observed(
            service,
            "rest",
            "read",
            service.read(body.cmd_hash, body.lines_count, body.offset, agent_id=body.agent_id),
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(body.agent_id)
        return result

    @router.post("/cancel", operation_id="cancelCommand", response_model=CancelResponse)
    async def cancel_command(body: CancelRequest):
        result = await observed(
            service, "rest", "cancel", service.cancel(body.cmd_hash, agent_id=body.agent_id)
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(body.agent_id)
        return result

    @router.get(
        "/health",
        operation_id="getTerminalHealth",
        response_model=HealthResponse,
        response_model_exclude_none=True,
    )
    async def terminal_health(agent_id: str | None = None):
        result = await observed(
            service, "rest", "health", service.health(auth_mode, agent_id=agent_id)
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(agent_id)
        return result

    return router

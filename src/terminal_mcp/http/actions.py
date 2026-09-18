# ruff: noqa: E501
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.api_models import (
    AgentFinishResponse,
    AgentOverviewResponse,
    CancelResponse,
    HealthResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
)
from terminal_mcp.core.service import ANONYMOUS_AGENT_ID, DEFAULT_READ_LINES, MAX_READ_LINES
from terminal_mcp.telemetry import observed

ScopeItem = Annotated[str, Field(min_length=1, max_length=80)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRequest(StrictRequest):
    agent_id: str = Field(min_length=1, max_length=64)


class OptionalAgentRequest(StrictRequest):
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentStartRequest(StrictRequest):
    task_summary: str = Field(min_length=1, max_length=120)
    intent: str = Field(min_length=1, max_length=160)
    work_scope: list[ScopeItem] = Field(min_length=1, max_length=4)


class AgentTaskRequest(AgentRequest):
    intent: str = Field(min_length=1, max_length=160)
    work_scope: list[ScopeItem] = Field(min_length=1, max_length=4)
    detail: str | None = Field(default=None, max_length=160)


class RunRequest(AgentRequest):
    cmd: str = Field(min_length=1, description="Shell script passed to /bin/bash -s through stdin.")


class ReadRequest(OptionalAgentRequest):
    cmd_hash: str | None = Field(default=None, min_length=8, max_length=8)
    lines_count: int = Field(default=DEFAULT_READ_LINES, ge=1, le=MAX_READ_LINES)
    offset: int | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if bool(self.agent_id) != bool(self.cmd_hash):
            raise ValueError(
                "agent_id and cmd_hash must be provided together, or both omitted for global terminal"
            )
        return self


class RecoveryRequest(OptionalAgentRequest):
    cmd: str = Field(min_length=1)


class CancelRequest(OptionalAgentRequest):
    cmd_hash: str = Field(min_length=8, max_length=8)


def build_actions_router(service, auth_mode="none"):
    router = APIRouter(prefix="/actions", tags=["terminal-actions"])

    @router.post(
        "/agent/start", operation_id="startAgentSession", response_model=AgentOverviewResponse
    )
    async def agent_start(body: AgentStartRequest):
        return await observed(
            service,
            "rest",
            "agent_start",
            service.agent_start(body.task_summary, body.intent, body.work_scope),
        )

    @router.post(
        "/agent/task", operation_id="updateAgentTask", response_model=AgentOverviewResponse
    )
    async def agent_task(body: AgentTaskRequest):
        return await observed(
            service,
            "rest",
            "agent_task",
            service.agent_task(body.agent_id, body.intent, body.work_scope, body.detail),
        )

    @router.post("/agents", operation_id="listActiveAgents", response_model=AgentOverviewResponse)
    async def agents(body: AgentRequest):
        return await observed(service, "rest", "agents", service.agents(body.agent_id))

    @router.post(
        "/agent/finish", operation_id="finishAgentSession", response_model=AgentFinishResponse
    )
    async def agent_finish(body: AgentRequest):
        return await observed(service, "rest", "agent_finish", service.agent_finish(body.agent_id))

    @router.post("/run", operation_id="runCommand", response_model=RunResponse)
    async def run_command(body: RunRequest):
        return await observed(service, "rest", "run", service.run(body.cmd, agent_id=body.agent_id))

    @router.post("/recovery", operation_id="recoveryCommand", response_model=RecoveryResponse)
    async def recovery_command(body: RecoveryRequest):
        result = await observed(
            service, "rest", "recovery", service.recovery(body.cmd, agent_id=body.agent_id)
        )
        result["agent_id"] = body.agent_id or ANONYMOUS_AGENT_ID
        return result

    @router.post("/read", operation_id="readTerminal", response_model=ReadResponse)
    async def read_terminal(body: ReadRequest):
        result = await observed(
            service,
            "rest",
            "read",
            service.read(body.cmd_hash, body.lines_count, body.offset, agent_id=body.agent_id),
        )
        result["agent_id"] = body.agent_id or ANONYMOUS_AGENT_ID
        return result

    @router.post("/cancel", operation_id="cancelCommand", response_model=CancelResponse)
    async def cancel_command(body: CancelRequest):
        result = await observed(
            service, "rest", "cancel", service.cancel(body.cmd_hash, agent_id=body.agent_id)
        )
        result["agent_id"] = body.agent_id or ANONYMOUS_AGENT_ID
        return result

    @router.get(
        "/health",
        operation_id="getTerminalHealth",
        response_model=HealthResponse,
        response_model_exclude_none=True,
    )
    async def terminal_health():
        result = await observed(service, "rest", "health", service.health(auth_mode))
        result["agent_id"] = ANONYMOUS_AGENT_ID
        return result

    return router

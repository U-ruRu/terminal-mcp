# ruff: noqa: E501
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.api_models import (
    AgentOverviewResponse,
    CancelResponse,
    ContextAction,
    ContextResponse,
    HealthResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
    TaskLane,
    TaskOperationalStatus,
    TasksResponse,
    TaskState,
)
from terminal_mcp.application import get_application
from terminal_mcp.core.orchestration import public_agent_name
from terminal_mcp.core.service import (
    DEFAULT_READ_LINES,
    MAX_READ_LINES,
    validate_context_request,
)
from terminal_mcp.telemetry import observed

ScopeItem = Annotated[str, Field(min_length=1, max_length=80)]
StepItem = Annotated[str, Field(min_length=1, max_length=160)]
TaskRef = Annotated[str, Field(min_length=1, max_length=512)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentRequest(StrictRequest):
    agent_id: str = Field(min_length=1, max_length=64)


class OptionalAgentRequest(StrictRequest):
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)


class AgentsRequest(OptionalAgentRequest):
    target: str | None = Field(default=None, min_length=1, max_length=64)
    history: bool = False
    target_session_ref: str | None = Field(default=None, min_length=1, max_length=128)
    show_details: bool = False
    show_intents: bool = False
    show_commands: bool = False
    command_hash: str | None = Field(default=None, min_length=8, max_length=8)
    since_minutes: int | None = Field(default=None, ge=1, le=10080)


class ContextRequest(StrictRequest):
    action: ContextAction
    id: int | None = Field(default=None, ge=1)
    summary: str | None = Field(default=None, min_length=1, max_length=100)
    content: str | None = Field(default=None, min_length=1)
    primary: bool | None = None
    show_details: bool = False

    @model_validator(mode="after")
    def validate_context(self):
        error = validate_context_request(
            self.action,
            context_id=self.id,
            summary=self.summary,
            content=self.content,
            primary=self.primary,
            show_details=self.show_details,
        )
        if error:
            raise ValueError(error)
        return self


class TasksRequest(StrictRequest):
    namespace: str | None = Field(default=None, min_length=1, max_length=120)
    task_id: str | None = Field(default=None, min_length=1, max_length=120)
    lane: TaskLane | None = None
    state: TaskState | None = None
    operational_status: TaskOperationalStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    show_details: bool = False
    show_done: bool = False
    show_archived: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    cursor: int | None = Field(default=None, ge=0)


class RunRequest(AgentRequest):
    cmd: str = Field(
        min_length=1, description="Script supplied to the configured shell through stdin."
    )
    queue_id: int | None = Field(default=None, ge=1)
    task_scope: str = Field(
        min_length=1,
        max_length=260,
        description=(
            "Required on every run. Use 'none' with no live claims; with live claims use "
            "'none', 'all', or one claimed '<namespace>/<task_id>'."
        ),
    )


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
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "agents",
                body.agent_id,
                target=body.target,
                show_details=body.show_details,
                show_intents=body.show_intents,
                show_commands=body.show_commands,
                command_hash=body.command_hash,
                since_minutes=body.since_minutes,
                history=body.history,
                target_session_ref=body.target_session_ref,
            ),
        )

    @router.post(
        "/context",
        operation_id="manageInstanceContext",
        response_model=ContextResponse,
        response_model_exclude_none=True,
    )
    async def context(body: ContextRequest):
        return await observed(
            service,
            "rest",
            "context",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "context",
                body.action,
                context_id=body.id,
                summary=body.summary,
                content=body.content,
                primary=body.primary,
                show_details=body.show_details,
            ),
        )

    @router.post(
        "/tasks",
        operation_id="listTasks",
        response_model=TasksResponse,
        response_model_exclude_none=True,
    )
    async def tasks(body: TasksRequest):
        return await observed(
            service,
            "rest",
            "tasks",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "tasks",
                namespace=body.namespace,
                task_id=body.task_id,
                lane=body.lane,
                state=body.state,
                operational_status=body.operational_status,
                tags=body.tags,
                show_details=body.show_details,
                show_done=body.show_done,
                show_archived=body.show_archived,
                limit=body.limit,
                cursor=body.cursor,
            ),
        )

    @router.post("/run", operation_id="runCommand", response_model=RunResponse)
    async def run_command(body: RunRequest):
        return await observed(
            service,
            "rest",
            "run",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "run",
                body.cmd,
                agent_id=body.agent_id,
                queue_id=body.queue_id,
                task_scope=body.task_scope,
            ),
        )

    @router.post("/recovery", operation_id="recoveryCommand", response_model=RecoveryResponse)
    async def recovery_command(body: RecoveryRequest):
        result = await observed(
            service,
            "rest",
            "recovery",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "recovery",
                body.cmd,
                agent_id=body.agent_id,
            ),
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
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "read",
                body.cmd_hash,
                body.lines_count,
                body.offset,
                agent_id=body.agent_id,
            ),
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(body.agent_id)
        return result

    @router.post("/cancel", operation_id="cancelCommand", response_model=CancelResponse)
    async def cancel_command(body: CancelRequest):
        result = await observed(
            service,
            "rest",
            "cancel",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "cancel",
                body.cmd_hash,
                agent_id=body.agent_id,
            ),
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
            service,
            "rest",
            "health",
            get_application(service).operator_data.call(
                actor_for(service, transport="http", endpoint_role="operator"),
                "health",
                auth_mode,
                agent_id=agent_id,
            ),
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(agent_id)
        return result

    return router

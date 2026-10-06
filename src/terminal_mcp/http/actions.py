# ruff: noqa: E501
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.api_models import (
    AgentFinishResponse,
    AgentOverviewResponse,
    CancelResponse,
    ContextAction,
    ContextResponse,
    CoordinateResponse,
    HealthResponse,
    MessageResponse,
    ReadResponse,
    RecoveryResponse,
    ReviewDimension,
    ReviewVerdict,
    RunResponse,
    TaskAction,
    TaskLane,
    TaskMutationResponse,
    TaskOperationalStatus,
    TaskPriority,
    TasksResponse,
    TaskState,
)
from terminal_mcp.application import get_application
from terminal_mcp.core.orchestration import public_agent_name, validate_message_routing
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
                raise ValueError("new admission requires task_summary, intent and details")
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
    target: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Public agent name, or 'broadcast' to send to all other active agents. ALERT requires an explicit destination.",
    )
    message_hash: str | None = Field(default=None, min_length=8, max_length=8)
    require_reply: bool = False
    alert: bool = Field(
        default=False,
        description="ALERT requires an explicit destination: target, target='broadcast', or namespace+task_id.",
    )
    namespace: str | None = Field(default=None, min_length=1, max_length=120)
    task_id: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_message(self):
        if self.message_hash is not None:
            if (
                self.target is not None
                or self.require_reply
                or self.alert
                or self.namespace is not None
                or self.task_id is not None
            ):
                raise ValueError("acknowledgement/reply inherits target and message policy")
        elif self.text is None:
            raise ValueError("sending requires text")
        routing_error = validate_message_routing(
            target=self.target,
            namespace=self.namespace,
            task_id=self.task_id,
            alert=self.alert,
        )
        if routing_error:
            raise ValueError(routing_error)
        return self


class AgentsRequest(OptionalAgentRequest):
    target: str | None = Field(default=None, min_length=1, max_length=64)
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


class TaskRequest(AgentRequest):
    action: TaskAction
    namespace: str = Field(min_length=1, max_length=120)
    task_id: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=200)
    lane: TaskLane | None = None
    priority: TaskPriority | None = None
    state: TaskState | None = Field(
        default=None,
        description=(
            "Workflow state change for an existing task; requires the caller to be its current live owner."
        ),
    )
    description: str | None = Field(default=None, max_length=8000)
    next_action: str | None = Field(default=None, max_length=2000)
    isolation_hint: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="Required for action=create; stored verbatim after trimming and never interpreted.",
    )
    resource_context: dict[str, object] | None = None
    cooperative: bool | None = Field(
        default=None,
        description=(
            "Controls concurrent participation, not task visibility. The first live claimant "
            "is owner; later live claimants are participants when cooperative=true."
        ),
    )
    checkpoint: str | dict[str, object] | list[object] | None = None
    candidate_ref: str | None = Field(default=None, max_length=200)
    input_refs: list[TaskRef] | None = Field(default=None, max_length=64)
    output_refs: list[TaskRef] | None = Field(default=None, max_length=64)
    dimensions: list[ReviewDimension] | None = Field(default=None, max_length=3)
    verdict: ReviewVerdict | None = None
    evidence: dict[str, object] | None = None
    result: str | dict[str, object] | list[object] | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    dependencies: list[dict[str, str]] | None = Field(default=None, max_length=100)
    force: bool = Field(
        default=False,
        description="Conscious dependency override for claim or terminal completion when genuinely necessary.",
    )
    force_reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Required justification when force=true; force never overrides ownership.",
    )
    claim_intent: str | None = Field(default=None, max_length=160)
    blocker_reason: str | None = Field(default=None, max_length=4000)
    release_reason: str | None = Field(
        default=None,
        max_length=4000,
        description="Required for release and retained as durable handoff history.",
    )
    archive_note: str | None = Field(default=None, max_length=4000)
    comment_text: str | None = Field(default=None, max_length=4000)
    relation_kind: str | None = Field(default=None, max_length=64)
    related_namespace: str | None = Field(default=None, max_length=120)
    related_task_id: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_isolation_hint(self):
        if self.action == "create" and self.isolation_hint is None:
            raise ValueError("isolation_hint is required for action=create")
        if self.action != "create" and self.isolation_hint is not None:
            raise ValueError("isolation_hint is accepted only for action=create")
        return self


class RunRequest(AgentRequest):
    cmd: str = Field(min_length=1, description="Shell script passed to /bin/bash -s through stdin.")
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "agent_start",
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "coordinate",
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "message",
                body.agent_id,
                text=body.text,
                target=body.target,
                message_hash=body.message_hash,
                require_reply=body.require_reply,
                alert=body.alert,
                namespace=body.namespace,
                task_id=body.task_id,
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "agents",
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
        return await observed(
            service,
            "rest",
            "agent_finish",
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "agent_finish",
                body.agent_id,
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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

    @router.post(
        "/task",
        operation_id="mutateTask",
        response_model=TaskMutationResponse,
        response_model_exclude_none=True,
    )
    async def task(body: TaskRequest):
        payload = body.model_dump(exclude={"agent_id", "action", "namespace"}, exclude_none=True)
        return await observed(
            service,
            "rest",
            "task",
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "task",
                body.agent_id,
                action=body.action,
                namespace=body.namespace,
                **payload,
            ),
        )

    @router.post("/run", operation_id="runCommand", response_model=RunResponse)
    async def run_command(body: RunRequest):
        return await observed(
            service,
            "rest",
            "run",
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
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
            get_application(service).compatibility.call(
                actor_for(service, transport="http", endpoint_role="legacy"),
                "health",
                auth_mode,
                agent_id=agent_id,
            ),
        )
        result.pop("agent_id", None)
        result["agent_name"] = public_agent_name(agent_id)
        return result

    return router

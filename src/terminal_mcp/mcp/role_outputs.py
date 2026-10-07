"""Typed public output contracts for versioned role MCP endpoints."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter

from terminal_mcp.core.task_projections import (
    Cursor,
    TaskDetail,
    TaskEventHistory,
    TaskListItem,
    TaskOutputHistory,
    TaskReceipt,
    TaskReviewHistory,
    TaskSnapshot,
    TaskWorkingSet,
    WorkflowWarning,
)
from terminal_mcp.mcp.output_contracts import (
    AccessError,
    CmdOutput,
    HealthResult,
    MessageOutput,
    SessionOutput,
    TaskOutput,
    projected_result,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _schema(annotation: Any) -> dict[str, Any]:
    schema = TypeAdapter(annotation).json_schema()
    schema.setdefault("type", "object")
    return schema


class _RoleOutput:
    __success_type__: ClassVar[Any]

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _schema(cls.__success_type__)


class TaskListSuccess(_Strict):
    ok: Literal[True]
    tasks: list[TaskListItem]
    next_cursor: Cursor | None = None


TaskListWire = TaskListSuccess | AccessError


class TaskListOutput(_RoleOutput, RootModel[TaskListWire]):
    __success_type__ = TaskListSuccess


class TaskClaimSuccess(_Strict):
    ok: Literal[True]
    action: Literal["claim"]
    task: TaskWorkingSet
    warnings: list[WorkflowWarning] = Field(default_factory=list)


class TaskReleaseSuccess(_Strict):
    ok: Literal[True]
    action: Literal["release"]
    task: TaskReceipt
    warnings: list[WorkflowWarning] = Field(default_factory=list)


TaskClaimSuccessWire = Annotated[
    TaskClaimSuccess | TaskReleaseSuccess, Field(discriminator="action")
]


class TaskClaimOutput(_RoleOutput, RootModel[TaskClaimSuccessWire | AccessError]):
    __success_type__ = TaskClaimSuccessWire


class TaskGetSnapshotSuccess(_Strict):
    ok: Literal[True]
    detail: Literal["snapshot"]
    task: TaskSnapshot


class TaskGetDetailSuccess(_Strict):
    ok: Literal[True]
    detail: Literal["detail"]
    task: TaskDetail


TaskHistoryPage = TaskEventHistory | TaskReviewHistory | TaskOutputHistory


class TaskGetHistorySuccess(_Strict):
    ok: Literal[True]
    detail: Literal["history"]
    task: TaskHistoryPage


TaskGetSuccess = Annotated[
    TaskGetSnapshotSuccess | TaskGetDetailSuccess | TaskGetHistorySuccess,
    Field(discriminator="detail"),
]


class TaskGetOutput(_RoleOutput, RootModel[TaskGetSuccess | AccessError]):
    __success_type__ = TaskGetSuccess


class TaskGraphNode(_Strict):
    namespace: str
    task_id: str
    state: str | None = None


class TaskGraphEdge(_Strict):
    direction: Literal["incoming", "outgoing"]
    kind: str
    namespace: str
    task_id: str
    state: str | None = None
    satisfied: bool | None = None


class TaskGraphSuccess(_Strict):
    ok: Literal[True]
    nodes: list[TaskGraphNode]
    edges: list[TaskGraphEdge]
    next_cursor: Cursor | None = None


class TaskGraphOutput(_RoleOutput, RootModel[TaskGraphSuccess | AccessError]):
    __success_type__ = TaskGraphSuccess


class LogicalAgentView(_Strict):
    logical_agent_id: str
    public_name: str
    authority_node_id: str


class WorkWindowView(_Strict):
    work_window_id: str
    state: str
    opened_at: str
    hard_expires_at: str
    remaining_seconds: int = Field(ge=0)
    revision: int = Field(ge=1)


class WorkSessionView(_Strict):
    work_session_id: str
    session_epoch: int = Field(ge=1)
    state: str
    started_at: str
    hard_expires_at: str
    role: Literal["executor", "coordinator", "legacy"]
    contract_version: int = Field(ge=1)


class AgentObserveSuccess(_Strict):
    ok: Literal[True]
    logical_agent: LogicalAgentView
    work_window: WorkWindowView
    work_session: WorkSessionView


class AgentObserveOutput(_RoleOutput, RootModel[AgentObserveSuccess | AccessError]):
    __success_type__ = AgentObserveSuccess


class CompactHealthTerminal(_Strict):
    ok: bool | None = None
    scheduler: str | None = None
    parallelism: int | None = None
    queue_size: int | None = None
    degraded: bool | None = None


class CompactHealthWorkflow(_Strict):
    ok: bool | None = None
    active_claims: int | None = None
    unreleased_claims: int | None = None
    stale_claims: int | None = None


class CompactHealthComponent(_Strict):
    id: str
    status: Literal["healthy", "degraded", "failed"]
    reason: str | None = None


class CompactHealthSuccess(_Strict):
    ok: bool
    application: str | None = None
    version: str | None = None
    storage: str | None = None
    status: Literal["healthy", "degraded", "failed"] | None = None
    components: list[CompactHealthComponent] = Field(default_factory=list)
    terminal: CompactHealthTerminal = Field(default_factory=CompactHealthTerminal)
    workflow: CompactHealthWorkflow = Field(default_factory=CompactHealthWorkflow)


RoleHealthSuccess = CompactHealthSuccess | HealthResult


class RoleHealthOutput(_RoleOutput, RootModel[RoleHealthSuccess | AccessError]):
    __success_type__ = RoleHealthSuccess


ROLE_OUTPUT_MODELS = {
    ("executor", "session"): SessionOutput,
    ("executor", "task_list"): TaskListOutput,
    ("executor", "command_run"): CmdOutput,
    ("executor", "command_read"): CmdOutput,
    ("executor", "command_cancel"): CmdOutput,
    ("executor", "command_recovery"): CmdOutput,
    ("executor", "task_claim"): TaskClaimOutput,
    ("executor", "task_state"): TaskOutput,
    ("executor", "task_comment"): TaskOutput,
    ("executor", "message"): MessageOutput,
    ("coordinator", "session"): SessionOutput,
    ("coordinator", "task_get"): TaskGetOutput,
    ("coordinator", "task_list"): TaskListOutput,
    ("coordinator", "task_manage"): TaskOutput,
    ("coordinator", "task_graph"): TaskGraphOutput,
    ("coordinator", "agent_observe"): AgentObserveOutput,
    ("coordinator", "message"): MessageOutput,
    ("coordinator", "health"): RoleHealthOutput,
}


def install_role_output_contract(mcp, role: str) -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    for (endpoint_role, tool_name), output_model in ROLE_OUTPUT_MODELS.items():
        if endpoint_role != role:
            continue
        tool = tools[tool_name]
        raw_fn = tool.fn

        async def contracted(*, _raw_fn=raw_fn, _model=output_model, _name=tool_name, **kwargs):
            raw = await _raw_fn(**kwargs)
            return projected_result(raw, _model, tool=_name, variant=role)

        tool.fn = contracted
        tool.fn_metadata.output_model = output_model
        tool.fn_metadata.output_schema = output_model.success_schema()
        tool.fn_metadata.wrap_output = False

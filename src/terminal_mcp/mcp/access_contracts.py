"""Access issuer and local role-attachment contracts; metadata is never an input."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from terminal_mcp.core.read_contract import DEFAULT_CMD_READ_LINES, MAX_CMD_READ_LINES
from terminal_mcp.mcp.output_contracts import AccessError, CmdReadResult
from terminal_mcp.mcp.role_contracts import (
    Cursor,
    Hash,
    MessageInput,
    Namespace,
    ResultValue,
    StrictRoleInput,
    TaskId,
)


class AttachInput(StrictRoleInput):
    action: Literal["attach", "intent"] = "attach"
    session_number: Annotated[
        str | None,
        Field(
            pattern=r"^[0-9]{4}$",
            description="Required for attach only. Four digits, including leading zeros.",
        ),
    ] = None
    intent: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=1000,
            description="Required for action=intent; optional initial intent on attach.",
        ),
    ] = None

    @model_validator(mode="after")
    def action_fields(self):
        if self.action == "attach" and self.session_number is None:
            raise ValueError("session_number is required for attach")
        if self.action == "intent" and (self.intent is None or not self.intent.strip()):
            raise ValueError("intent is required for refresh")
        if self.action == "intent" and self.session_number is not None:
            raise ValueError("intent refresh uses the existing connector binding")
        if self.intent is not None and not self.intent.strip():
            raise ValueError("intent must contain text")
        return self


class MeshAgentObserveInput(StrictRoleInput):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: Cursor | None = None
    include_inactive: bool = False
    logical_agent_id: Annotated[str | None, Field(min_length=1, max_length=160)] = None


class IssuerSessionInput(StrictRoleInput):
    action: Literal["start", "end"]


class GetEventsInput(StrictRoleInput):
    event_id: Annotated[
        str | None, Field(pattern=r"^evt_[0-9a-f]{32}$", max_length=36)
    ] = None
    limit: Annotated[int, Field(ge=1, le=20)] = 20
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    show_read: bool = False

    @model_validator(mode="after")
    def validate_selector(self):
        if self.event_id is not None and self.cursor is not None:
            raise ValueError("event_id and cursor are mutually exclusive")
        return self


class MeshCommandReadInput(StrictRoleInput):
    cmd_hash: Hash | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_CMD_READ_LINES)] = DEFAULT_CMD_READ_LINES
    cursor: Cursor | None = None


class MeshTaskCommentInput(StrictRoleInput):
    action: Literal["comment", "checkpoint"] = "comment"
    namespace: Namespace
    task_id: TaskId
    comment_text: Annotated[
        str | None, Field(min_length=1, max_length=4000, description="Required for action=comment.")
    ] = None
    checkpoint: Annotated[
        ResultValue | None,
        Field(
            description="Required for action=checkpoint; saves progress and preserves task state."
        ),
    ] = None

    def to_request(self):
        from terminal_mcp.application.task_requests import TaskCheckpointRequest, TaskCommentRequest

        model = TaskCheckpointRequest if self.action == "checkpoint" else TaskCommentRequest
        return model.model_validate({"action": self.action, **self.model_dump(exclude_unset=True)})

    @model_validator(mode="after")
    def action_fields(self):
        self.to_request()
        return self


class MeshMessageInput(MessageInput):
    scope: Literal["local", "fleet"] = "local"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeshCycle(_Strict):
    state: Literal[
        "active", "warning", "draining", "expired", "cooldown", "idle", "suspended", "deleted"
    ]
    started_at: str | None
    hard_expires_at: str | None
    remaining_seconds: int = Field(ge=0)
    rearm_at: str | None


class MeshPolicy(_Strict):
    duration_seconds: int = Field(ge=1)
    cooldown_seconds: int = Field(ge=0)
    rearm_enabled: bool
    warning_seconds: int = Field(ge=0)
    draining_seconds: int = Field(ge=0)
    release_on_end: bool | None = None


class MeshLocalSession(_Strict):
    ok: Literal[True]
    action: Literal["attach", "status"]
    attached: bool
    issuer_node_id: str
    slot_id: str
    logical_agent_id: str
    authority_node_id: str
    public_name: str
    mode: Literal["legacy", "persistent"]
    slot_kind: Literal["legacy", "persistent"]
    slot_state: Literal["active", "suspended", "deleted"]
    session_lifecycle: MeshCycle
    cleanup_pending: bool
    hard_expires_at: str | None
    work_session_id: str | None = None
    session_epoch: int | None = None
    role: Literal["executor", "coordinator"]
    contract_version: int


class MeshAttachReceipt(_Strict):
    ok: Literal[True]


class MeshIntentReceipt(_Strict):
    ok: Literal[True]
    intent_updated_at: str
    refresh_due_at: str


class MeshSessionOutput(RootModel[MeshAttachReceipt | MeshIntentReceipt | AccessError]):
    __success_type__: ClassVar = MeshAttachReceipt | MeshIntentReceipt


class IssuerStartReceipt(_Strict):
    ok: Literal[True]
    session_number: Annotated[str, Field(pattern=r"^[0-9]{4}$")]


class IssuerEndReceipt(_Strict):
    ok: Literal[True]


class IssuerOutput(RootModel[IssuerStartReceipt | IssuerEndReceipt | AccessError]):
    __success_type__: ClassVar = IssuerStartReceipt | IssuerEndReceipt


class AccessEventItem(_Strict):
    eventId: str
    name: Literal["agent.attached", "agent.inactive", "session.ended", "message.received"]
    timestamp: str
    data: dict[str, object]
    read: bool


class AccessEventsReceipt(_Strict):
    ok: Literal[True]
    events: list[AccessEventItem]
    next_cursor: str | None
    has_more: bool


class AccessGetEventsOutput(RootModel[AccessEventsReceipt | AccessError]):
    __success_type__: ClassVar = AccessEventsReceipt


class CommandJournalItem(_Strict):
    cmd_hash: str
    created_at: str | None = None
    actor: str | None = None
    logical_agent_id: str | None = None
    issuer_node_id: str | None = None
    status: str
    queue_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None


class CommandJournal(_Strict):
    ok: Literal[True]
    action: Literal["journal"]
    node_id: str
    commands: list[CommandJournalItem]
    next_cursor: str | None


class MeshCommandReadOutput(RootModel[CmdReadResult | CommandJournal | AccessError]):
    __success_type__: ClassVar = CmdReadResult | CommandJournal


class MeshUnboundSession(_Strict):
    ok: Literal[True]
    action: Literal["status"]
    attached: Literal[False]
    authority_node_id: str
    role: Literal["executor", "coordinator"]
    contract_version: int


class MeshAgentView(_Strict):
    logical_agent_id: str
    display_name: str
    current_intent: str | None
    intent_updated_at: str | None
    intent_state: Literal["missing", "fresh", "stale"]
    activity_stale: bool
    work_session_id: str | None
    session_epoch: int | None
    session_state: str
    session_started_at: str | None
    hard_expires_at: str | None
    public_name: str
    last_server: str
    session_duration: int
    last_activity: str | None


class MeshAgentObserve(_Strict):
    ok: Literal[True]
    scope: Literal["local"]
    server_id: str
    agents: list[MeshAgentView]
    next_cursor: str | None = None


class MeshObserveOutput(RootModel[MeshAgentObserve | AccessError]):
    __success_type__: ClassVar = MeshAgentObserve

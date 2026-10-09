"""Access issuer and local role-attachment contracts; metadata is never an input."""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from terminal_mcp.core.read_contract import DEFAULT_CMD_READ_LINES, MAX_CMD_READ_LINES
from terminal_mcp.mcp.output_contracts import AccessError, CmdReadResult
from terminal_mcp.mcp.role_contracts import (
    Cursor,
    ExpectedRevision,
    Hash,
    MessageInput,
    Namespace,
    ResultValue,
    StrictRoleInput,
    TaskId,
)


class AttachInput(StrictRoleInput):
    action: Literal["attach"] = "attach"
    session_number: Annotated[str, Field(pattern=r"^[0-9]{4}$", description="Four-digit session number, including leading zeros.")]


class IssuerSessionInput(StrictRoleInput):
    action: Literal["start", "end"]


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
    expected_revision: ExpectedRevision | None = None

    def to_request(self):
        from terminal_mcp.application.task_requests import TaskCheckpointRequest, TaskCommentRequest

        model = TaskCheckpointRequest if self.action == "checkpoint" else TaskCommentRequest
        return model.model_validate({"action": self.action, **self.model_dump(exclude_unset=True)})

    @model_validator(mode="after")
    def action_fields(self):
        self.to_request()
        return self


class MeshMessageInput(MessageInput):
    scope: Literal["local", "fleet"] = "fleet"


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


class MeshSessionOutput(RootModel[MeshAttachReceipt | AccessError]):
    __success_type__: ClassVar = MeshAttachReceipt


class IssuerStartReceipt(_Strict):
    ok: Literal[True]
    session_number: Annotated[str, Field(pattern=r"^[0-9]{4}$")]


class IssuerEndReceipt(_Strict):
    ok: Literal[True]


class IssuerOutput(RootModel[IssuerStartReceipt | IssuerEndReceipt | AccessError]):
    __success_type__: ClassVar = IssuerStartReceipt | IssuerEndReceipt


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
    public_name: str
    last_server: str
    session_duration: int
    last_activity: str | None


class MeshAgentObserve(_Strict):
    ok: Literal[True]
    agents: list[MeshAgentView]


class MeshObserveOutput(RootModel[MeshAgentObserve | AccessError]):
    __success_type__: ClassVar = MeshAgentObserve

from typing import Literal

from pydantic import BaseModel, Field

CommandStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "not_found",
]


class SessionStatus(BaseModel):
    agent_name: str | None = None
    session_expired: bool | None = None
    registration_required: bool | None = None
    task_context_expired: bool | None = None
    task_age_seconds: int | None = None
    max_task_age_seconds: int | None = None
    coordination_message_pending: bool | None = None
    pending_messages: list[str] = Field(
        default_factory=list,
        description=(
            "Unread coordination messages for this agent. Read each message and acknowledge it "
            "with message(agent_id, message_hash=...) before starting a new run."
        ),
    )


class RunResponse(SessionStatus):
    ok: bool
    cmd_hash: str | None = None
    active_agents: list[str] = Field(default_factory=list)
    error: str | None = None


class ReadResponse(SessionStatus):
    ok: bool
    active_agents: list[str] = Field(default_factory=list)
    lines: list[str]
    next_offset: int
    overall_lines_count: int | None = None
    displayed_lines_count: int
    cmd_hash: str | None = None
    status: CommandStatus | None = None
    exit_code: int | None = None
    error: str | None = None


class RecoveryResponse(SessionStatus):
    ok: bool
    cmd_hash: str | None = None
    lines: list[str]
    overall_lines_count: int
    displayed_lines_count: int
    exit_code: int | None = None
    error: str | None = None
    duration_ms: int


class HealthCommandResult(BaseModel):
    command: str
    lines: list[str]
    status: CommandStatus
    exit_code: int | None = None
    error: str | None = None
    ok: bool
    duration_ms: int


class CancelResponse(SessionStatus):
    ok: bool
    cmd_hash: str
    error: str | None = None


class TerminalHealth(BaseModel):
    ok: bool
    user: str
    uid: int
    gid: int
    cwd: str
    privilege: str
    shell: str
    terminal_user: str
    scheduler: str
    parallelism: int
    queue_size: int
    running_commands: list[str]


class HealthResponse(SessionStatus):
    ok: bool
    application: str
    storage: str
    auth_mode: str
    terminal: TerminalHealth
    custom_command: HealthCommandResult | None = None


class RecentCommand(BaseModel):
    created_at: str
    command_hash: str
    preview: str
    status: str
    command_type: str


class AgentSelf(BaseModel):
    name: str
    agent_id: str | None = None
    ttl_seconds: int
    task_lease_seconds: int
    task_summary: str
    intent: str
    work_scope: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    current_step: int


class ActiveAgent(BaseModel):
    name: str
    idle_seconds: int
    task_summary: str
    intent: str
    work_scope: list[str] = Field(default_factory=list)
    current_step: int
    recent_commands: list[RecentCommand]


class ScopeOverlap(BaseModel):
    name: str
    scope: str


class AgentOverviewResponse(SessionStatus):
    ok: bool
    self: AgentSelf | None = None
    active: list[ActiveAgent] = Field(default_factory=list)
    overlaps: list[ScopeOverlap] = Field(default_factory=list)
    additional_active_agents: int = 0
    detail: str | None = None
    error: str | None = None




class CoordinateResponse(SessionStatus):
    ok: bool
    step: int | None = None
    intent: str | None = None
    detail: str | None = None
    other_details: list[str] = Field(default_factory=list)
    active_agents: list[str] = Field(default_factory=list)
    error: str | None = None


class MessageResponse(SessionStatus):
    ok: bool
    message_hash: str | None = None
    delivered_to: list[str] = Field(default_factory=list)
    read_by: list[str] = Field(default_factory=list)
    error: str | None = None

class AgentFinishResponse(SessionStatus):
    ok: bool
    finished: bool | None = None
    error: str | None = None

from typing import Literal

from pydantic import BaseModel

CommandStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "not_found",
]


class SessionStatus(BaseModel):
    agent_id: str | None = None
    session_expired: bool | None = None
    registration_required: bool | None = None


class RunResponse(SessionStatus):
    ok: bool
    cmd_hash: str | None = None
    error: str | None = None


class ReadResponse(SessionStatus):
    ok: bool
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
    agent_id: str
    ttl_seconds: int
    task_summary: str
    intent: str
    work_scope: list[str]


class ActiveAgent(BaseModel):
    agent_id: str
    idle_seconds: int
    task_summary: str
    intent: str
    work_scope: list[str]
    recent_commands: list[RecentCommand]


class ScopeOverlap(BaseModel):
    agent_id: str
    scope: str


class AgentOverviewResponse(SessionStatus):
    ok: bool
    self: AgentSelf | None = None
    active: list[ActiveAgent] = []
    overlaps: list[ScopeOverlap] = []
    additional_active_agents: int = 0
    detail: str | None = None
    error: str | None = None


class AgentFinishResponse(SessionStatus):
    ok: bool
    finished: bool | None = None
    error: str | None = None

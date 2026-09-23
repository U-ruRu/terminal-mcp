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
    session_status: str | None = None
    session_started_at: str | None = None
    session_age_seconds: int | None = None
    session_remaining_seconds: int | None = None
    session_warning: str | None = None
    session_end_reason: str | None = None
    task_context_expired: bool | None = None
    task_age_seconds: int | None = None
    max_task_age_seconds: int | None = None
    preferred_queue_id: int | None = None
    coordination_message_pending: bool | None = None
    unread_message_pending: bool | None = None
    reply_required_pending: bool | None = None
    alert_pending: bool | None = None
    pending_messages: list[str] = Field(default_factory=list)
    alert_messages: list[str] = Field(default_factory=list)
    reply_required_messages: list[str] = Field(default_factory=list)


class RunResponse(SessionStatus):
    ok: bool
    cmd_hash: str | None = None
    queue_id: int | None = None
    queue_position: int | None = None
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
    queue_id: int | None = None
    queue_position: int | None = None
    output_truncated: bool | None = None
    output_retained: bool | None = None
    output_pruned_at: str | None = None
    output_bytes: int | None = None
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
    output_truncated: bool | None = None
    output_retained: bool | None = None
    output_pruned_at: str | None = None
    output_bytes: int | None = None


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


class QueueHealth(BaseModel):
    queue_id: int
    running: str | None = None
    queued: int


class OutputCacheHealth(BaseModel):
    used_bytes: int
    target_bytes: int
    max_bytes: int
    lines: int
    max_lines: int
    retained_commands: int
    truncated_commands: int
    allocated_bytes: int
    live_page_bytes: int
    last_prune_at: str | None = None


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
    queues: list[QueueHealth] = Field(default_factory=list)
    worker_health: dict[str, bool] = Field(default_factory=dict)
    output_cache: OutputCacheHealth | None = None


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
    queue_id: int | None = None
    queue_sequence: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


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
    preferred_queue_id: int | None = None


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


class MessageJournalEntry(BaseModel):
    message_hash: str
    state: str
    sender_name: str
    text: str
    require_reply: bool = False
    alert: bool = False
    created_at: str
    first_seen_at: str | None = None
    read_at: str | None = None
    replied_at: str | None = None
    seen_count: int = 0


class AgentSessionRecord(BaseModel):
    name: str
    status: str
    last_activity: str
    last_activity_at: str
    last_activity_tool: str | None = None
    last_activity_command_hash: str | None = None
    intent: str
    current_step: int
    preferred_queue_id: int | None = None
    last_command: RecentCommand | None = None
    end_reason: str | None = None
    messages_awaiting_read: int = 0
    messages_awaiting_reply: int = 0
    alerts_pending: int = 0
    message_journal: list[MessageJournalEntry] = Field(default_factory=list)
    task_summary: str | None = None
    details: list[str] | None = None
    work_scope: list[str] | None = None
    registered_at: str | None = None
    ended_at: str | None = None


class IntentJournalEntry(BaseModel):
    timestamp: str
    intent: str
    step: int
    work_scope: list[str] = Field(default_factory=list)


class CommandDetail(BaseModel):
    command_hash: str
    cmd: str
    status: str
    queue_id: int | None = None
    queue_sequence: int | None = None
    enqueued_at: str | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    agent_name: str | None = None
    command_type: str | None = None
    created_at: str | None = None


class AgentOverviewResponse(SessionStatus):
    ok: bool
    self: AgentSelf | None = None
    active: list[ActiveAgent] = Field(default_factory=list)
    sessions: list[AgentSessionRecord] = Field(default_factory=list)
    intent_journal: list[IntentJournalEntry] = Field(default_factory=list)
    command_journal: list[RecentCommand] = Field(default_factory=list)
    command: CommandDetail | None = None
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
    reply_message_hash: str | None = None
    delivered_to: list[str] = Field(default_factory=list)
    seen_by: list[str] = Field(default_factory=list)
    read_by: list[str] = Field(default_factory=list)
    replied_by: list[str] = Field(default_factory=list)
    error: str | None = None


class AgentFinishResponse(SessionStatus):
    ok: bool
    finished: bool | None = None
    error: str | None = None

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

ContextAction = Literal["list", "create", "update", "delete"]

TaskAction = Literal[
    "create",
    "claim",
    "release",
    "update",
    "checkpoint",
    "comment",
    "relate",
    "unrelate",
    "state",
    "done",
    "archive",
    "review",
]
TaskLane = Literal["implementation", "review", "release", "integration", "general"]
TaskState = Literal["ready", "in_progress", "qa", "blocked", "deferred", "done"]
TaskOperationalStatus = Literal["ready", "in_progress", "qa", "blocked", "deferred", "done"]
TaskPriority = Literal["P0", "P1", "P2", "P3"]
ReviewDimension = Literal["A", "C", "R"]
ReviewVerdict = Literal["NON_BLOCKING", "BLOCKING"]


class SessionStatus(BaseModel):
    agent_name: str | None = None
    session_expired: bool | None = None
    registration_required: bool | None = None
    admission_required: bool | None = None
    proposed_agent_id: str | None = None
    return_to_chat: bool | None = None
    session_status: str | None = None
    session_started_at: str | None = None
    session_age_seconds: int | None = None
    session_remaining_seconds: int | None = None
    session_warning: str | None = None
    session_end_reason: str | None = None
    task_context_expired: bool | None = None
    task_age_seconds: int | None = Field(
        default=None, description="Compatibility alias for task_context_age_seconds."
    )
    max_task_age_seconds: int | None = Field(
        default=None, description="Compatibility alias for task_context_ttl_seconds."
    )
    task_context_age_seconds: int | None = Field(
        default=None, description="Canonical age of the current task context in seconds."
    )
    task_context_ttl_seconds: int | None = Field(
        default=None, description="Canonical freshness TTL for the current task context."
    )
    preferred_queue_id: int | None = None
    coordination_message_pending: bool | None = None
    unread_message_pending: bool | None = None
    reply_required_pending: bool | None = None
    alert_pending: bool | None = None
    pending_messages: list[str] | None = None
    alert_messages: list[str] | None = None
    reply_required_messages: list[str] | None = None
    message_grace_remaining_seconds: int | None = None
    task_scope_options: list[str] | None = Field(
        default=None, description="Current legal values for run.task_scope for this agent session."
    )


class RunResponse(SessionStatus):
    ok: bool
    cmd_hash: str | None = None
    queue_id: int | None = None
    queue_position: int | None = None
    task_scope: str | None = Field(
        default=None, description="Explicit task provenance scope selected for this run."
    )
    task_targets: list[str] | None = Field(
        default=None, description="Managed tasks that received command provenance events."
    )
    active_agents: list[str] | None = None
    error: str | None = None


class ReadResponse(SessionStatus):
    ok: bool
    active_agents: list[str] | None = None
    lines: list[str]
    next_offset: int
    overall_lines_count: int | None = None
    displayed_lines_count: int
    cmd_hash: str | None = None
    status: CommandStatus | None = None
    exit_code: int | None = None
    queue_id: int | None = None
    execution_started: bool | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
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
    cancelled_from: Literal["queued", "running"] | None = None
    execution_started: bool | None = None


class QueueHealth(BaseModel):
    queue_id: int
    running: str | None = None
    durable_running: str | None = None
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
    finalization_pending_commands: list[str] = Field(default_factory=list)
    stale_running_commands: list[str] = Field(default_factory=list)
    unowned_running_commands: list[str] = Field(default_factory=list)
    degraded: bool = False
    queues: list[QueueHealth] = Field(default_factory=list)
    worker_health: dict[str, bool] = Field(default_factory=dict)
    output_cache: OutputCacheHealth | None = None


class HealthResponse(SessionStatus):
    ok: bool
    application: str
    version: str
    storage: str
    auth_mode: str
    terminal: TerminalHealth
    workflow: dict[str, object] | None = None
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


class WorkflowWarning(BaseModel):
    code: str
    severity: str = "warning"
    message: str
    task_id: str | None = None
    context: dict[str, object] = Field(default_factory=dict)


class ManagedTaskRef(BaseModel):
    namespace: str
    task_id: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus = Field(
        description=(
            "Derived runtime status; unclaimed ready work with open dependencies is blocked."
        )
    )
    isolation_hint: str
    claimed_at: str
    claim_age_seconds: int
    claim_intent: str
    role: Literal["owner", "participant"]


class ContextListEntry(BaseModel):
    id: int
    summary: str
    content: str | None = None


class ContextEntry(BaseModel):
    id: int
    summary: str
    content: str
    primary: bool


class ContextResponse(BaseModel):
    ok: bool
    primary: list[ContextListEntry] | None = None
    additional: list[ContextListEntry] | None = None
    entry: ContextEntry | None = None
    deleted_id: int | None = None
    error: str | None = None


class TaskClaimView(BaseModel):
    agent_name: str
    claimed_at: str
    claim_age_seconds: int
    claim_intent: str
    role: Literal["owner", "participant"]


class TaskCard(BaseModel):
    namespace: str
    task_id: str
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus = Field(
        description=(
            "Derived runtime status; unclaimed ready work with open dependencies is blocked."
        )
    )
    isolation_hint: str
    next_action: str = ""
    checkpoint: str | dict[str, object] = Field(default_factory=dict)
    candidate_ref: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    output_state_id: int | None = None
    output_states: list[dict[str, object]] | None = None
    result: str | dict[str, object] | list[object] | None = None
    tags: list[str] = Field(default_factory=list)
    state_changed_at: str | None = None
    ready_since: str | None = None
    archived_at: str | None = None
    archive_note: str | None = None
    revision: int = 1
    cooperative: bool = False
    active: bool = False
    claims: list[TaskClaimView] = Field(default_factory=list)
    owner: TaskClaimView | None = None
    participants: list[TaskClaimView] = Field(default_factory=list)
    blocking_dependencies: list[dict[str, object]] = Field(
        default_factory=list,
        description=(
            "Unsatisfied or missing dependencies that currently block ordinary claimability."
        ),
    )
    review_requirements: list[ReviewDimension] = Field(default_factory=list)
    description: str | None = None
    resource_context: dict[str, object] | None = None
    dependencies: list[dict[str, object]] | None = None
    relations: list[dict[str, object]] | None = None
    comments: list[dict[str, object]] | None = None
    reviews: list[dict[str, object]] | None = None
    events: list[dict[str, object]] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class TasksResponse(BaseModel):
    ok: bool
    summary: dict[str, object] | None = None
    recommended: dict[str, object] | None = None
    tag_counts: dict[str, int] = Field(default_factory=dict)
    tasks: list[TaskCard] = Field(default_factory=list)
    task: TaskCard | None = None
    next_cursor: int | None = None
    error: str | None = None


class TaskMutationResponse(SessionStatus):
    ok: bool
    task: TaskCard | None = None
    warnings: list[WorkflowWarning] = Field(default_factory=list)
    code: str | None = None
    blocking_dependencies: list[dict[str, object]] | None = None
    error: str | None = None


class AgentSelf(BaseModel):
    name: str
    agent_id: str | None = None
    ttl_seconds: int
    task_context_ttl_seconds: int | None = Field(
        default=None, description="Canonical task-context freshness TTL in seconds."
    )
    task_lease_seconds: int = Field(description="Compatibility alias for task_context_ttl_seconds.")
    task_summary: str
    intent: str
    work_scope: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    current_step: int
    preferred_queue_id: int | None = None
    managed_tasks: list[ManagedTaskRef] | None = None


class ActiveAgent(BaseModel):
    name: str
    status: str | None = None
    session_age_seconds: int | None = None
    idle_seconds: int
    intent: str
    current_step: int | None = None
    task_summary: str | None = None
    work_scope: list[str] | None = None
    recent_commands: list[RecentCommand] | None = None
    managed_tasks: list[ManagedTaskRef] | None = None


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
    idle_seconds: int | None = None
    last_activity_tool: str | None = None
    last_activity_command_hash: str | None = None
    intent: str
    current_step: int
    preferred_queue_id: int | None = None
    session_age_seconds: int | None = None
    last_command: RecentCommand | None = None
    end_reason: str | None = None
    messages_awaiting_read: int | None = None
    messages_awaiting_reply: int | None = None
    alerts_pending: int | None = None
    message_journal: list[MessageJournalEntry] | None = None
    task_summary: str | None = None
    details: list[str] | None = None
    work_scope: list[str] | None = None
    registered_at: str | None = None
    ended_at: str | None = None
    managed_tasks: list[ManagedTaskRef] | None = None


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
    primary_context: list[ContextEntry] | None = None
    active: list[ActiveAgent] = Field(default_factory=list)
    sessions: list[AgentSessionRecord] = Field(default_factory=list)
    intent_journal: list[IntentJournalEntry] | None = None
    command_journal: list[RecentCommand] | None = None
    command: CommandDetail | None = None
    overlaps: list[ScopeOverlap] | None = None
    additional_active_agents: int = 0
    detail: str | None = None
    error: str | None = None


class CoordinateResponse(SessionStatus):
    ok: bool
    step: int | None = None
    intent: str | None = None
    detail: str | None = None
    other_details: list[str] = Field(default_factory=list)
    active_agents: list[str] | None = None
    error: str | None = None


class MessageResponse(SessionStatus):
    ok: bool
    message_hash: str | None = None
    reply_message_hash: str | None = None
    namespace: str | None = None
    task_id: str | None = None
    delivered_to: list[str] = Field(default_factory=list)
    seen_by: list[str] = Field(default_factory=list)
    read_by: list[str] = Field(default_factory=list)
    replied_by: list[str] = Field(default_factory=list)
    inactive_recipients: list[str] = Field(
        default_factory=list,
        description=(
            "Recipients whose exact Agent Session is inactive, separate from receipt state."
        ),
    )
    error: str | None = None


class PendingCommunicationSummary(BaseModel):
    unacknowledged: list[str] = Field(default_factory=list)
    reply_required: list[str] = Field(default_factory=list)
    alerts: list[str] = Field(default_factory=list)


class AgentFinishResponse(SessionStatus):
    ok: bool
    finished: bool | None = None
    pending_communication: PendingCommunicationSummary | None = Field(
        default=None,
        description="Outstanding communication summary retained when agent_finish is allowed.",
    )
    error: str | None = None


class ConsoleSnapshotConsistency(BaseModel):
    mode: Literal["cursor_first_at_least_once"] = "cursor_first_at_least_once"
    high_water_seq: int = Field(ge=0)
    replay_from_seq: int = Field(ge=0)
    duplicate_events_possible: bool = True


class ConsoleSnapshotResponse(BaseModel):
    ok: bool
    high_water_seq: int = Field(ge=0)
    consistency: ConsoleSnapshotConsistency
    instance: dict[str, object]
    agents: dict[str, object]
    tasks: dict[str, object]
    contexts: dict[str, object]
    communications: list[dict[str, object]] = Field(default_factory=list)
    persistent: dict[str, object] | None = None
    error: str | None = None

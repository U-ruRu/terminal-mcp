from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, ValidationError

from terminal_mcp.core.public_errors import (
    MAX_COORDINATION_MESSAGES,
    MAX_ERROR_MESSAGE,
    MAX_ERROR_PATH,
    ErrorOutcome,
    ErrorRepair,
    RecoveryAction,
    normalize_public_error,
    public_error,
)
from terminal_mcp.core.read_contract import (
    CALL_TOOL_RESULT_BUDGET_BYTES,
    summary_message,
)
from terminal_mcp.core.task_projections import (
    Cursor as Cursor,
)
from terminal_mcp.core.task_projections import (
    JsonPayload as JsonPayload,
)
from terminal_mcp.core.task_projections import (
    Namespace as Namespace,
)
from terminal_mcp.core.task_projections import (
    TaskCheckpointSnapshot as TaskCheckpointSnapshot,
)
from terminal_mcp.core.task_projections import (
    TaskClaim as TaskClaim,
)
from terminal_mcp.core.task_projections import (
    TaskDependency as TaskDependency,
)
from terminal_mcp.core.task_projections import (
    TaskEvent as TaskEvent,
)
from terminal_mcp.core.task_projections import (
    TaskId as TaskId,
)
from terminal_mcp.core.task_projections import (
    TaskLane as TaskLane,
)
from terminal_mcp.core.task_projections import (
    TaskListItem as TaskListItem,
)
from terminal_mcp.core.task_projections import (
    TaskListSummary as TaskListSummary,
)
from terminal_mcp.core.task_projections import (
    TaskOperationalStatus as TaskOperationalStatus,
)
from terminal_mcp.core.task_projections import (
    TaskOutputState as TaskOutputState,
)
from terminal_mcp.core.task_projections import (
    TaskPriority as TaskPriority,
)
from terminal_mcp.core.task_projections import (
    TaskReceipt as TaskReceipt,
)
from terminal_mcp.core.task_projections import (
    TaskRecommendation as TaskRecommendation,
)
from terminal_mcp.core.task_projections import (
    TaskRecord as TaskRecord,
)
from terminal_mcp.core.task_projections import (
    TaskRelation as TaskRelation,
)
from terminal_mcp.core.task_projections import (
    TaskResourceContext as TaskResourceContext,
)
from terminal_mcp.core.task_projections import (
    TaskReview as TaskReview,
)
from terminal_mcp.core.task_projections import (
    TaskSnapshot as TaskSnapshot,
)
from terminal_mcp.core.task_projections import (
    TaskState as TaskState,
)
from terminal_mcp.core.task_projections import (
    WorkflowWarning as WorkflowWarning,
)
from terminal_mcp.core.task_projections import project_task_receipt


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessCode(RootModel[str]):
    root: Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]


class CommandId(RootModel[str]):
    root: Annotated[str, Field(min_length=1)]


class MessageId(RootModel[str]):
    root: Annotated[str, Field(min_length=1)]


class ContextId(RootModel[int]):
    root: Annotated[int, Field(ge=1)]


class SessionMode(StrEnum):
    persistent = "persistent"
    legacy = "legacy"


class SessionState(StrEnum):
    inactive = "inactive"
    active = "active"
    stopping = "stopping"
    interrupted = "interrupted"


class SessionLifecycleInfo(_Strict):
    state: Literal["active", "warning", "draining", "expired", "cooldown"]
    remaining_seconds: int = Field(ge=0)
    hard_expires_at: str
    return_to_chat: bool = False


class _SessionAware(_Strict):
    session_lifecycle: SessionLifecycleInfo | None = None


class CommandStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    not_found = "not_found"


class MessageMode(StrEnum):
    notify = "notify"
    ack = "ack"
    alert = "alert"


class MessageState(StrEnum):
    delivered = "delivered"
    seen = "seen"
    read = "read"
    acknowledged = "acknowledged"
    replied = "replied"


class AccessError(_Strict):
    ok: Literal[False]
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=MAX_ERROR_MESSAGE)
    error: str = Field(min_length=1, max_length=MAX_ERROR_MESSAGE)
    details: ErrorRepair | None = None
    outcome: ErrorOutcome
    retry: RecoveryAction
    reason: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    path: str | None = Field(default=None, max_length=MAX_ERROR_PATH)


def _success_schema(annotation: Any) -> dict[str, Any]:
    schema = TypeAdapter(annotation).json_schema()
    schema.setdefault("type", "object")
    return schema


class SessionInfo(_Strict):
    public_name: str
    mode: SessionMode
    session_state: SessionState | Literal["warning", "draining", "expired", "cooldown"]
    display_suffix: str | None = None
    authority_node_id: str | None = None
    access_generation: int | None = None
    session_ref: str | None = None
    work_session_id: str | None = None
    session_epoch: int | None = None
    role: Literal["legacy", "executor", "coordinator"] | None = None
    contract_version: int | None = Field(default=None, ge=1)
    state: SessionState | Literal["warning", "draining", "expired", "cooldown"] | None = None
    started_at: str | None = None
    hard_expires_at: str | None = None
    remaining_seconds: int | None = Field(default=None, ge=0)


class SessionStartResult(_Strict):
    ok: Literal[True]
    action: Literal["start"]
    session: SessionInfo
    access_code: AccessCode | None = None


class SessionEndResult(_Strict):
    ok: Literal[True]
    action: Literal["end"]
    session_state: Literal["inactive"]
    session_ref: str | None = None
    public_name: str | None = None
    mode: SessionMode | None = None
    stopping: bool | None = None


class SessionInterruptResult(_Strict):
    ok: Literal[True]
    action: Literal["interrupt"]
    session_state: Literal["interrupted"]
    session_ref: str | None = None
    public_name: str | None = None
    mode: SessionMode | None = None
    stopping: bool | None = None


SessionSuccess = Annotated[
    SessionStartResult | SessionEndResult | SessionInterruptResult,
    Field(discriminator="action"),
]


class SessionOutput(RootModel[SessionSuccess | AccessError]):
    __success_type__: ClassVar[Any] = SessionSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class SessionSummary(_Strict):
    public_name: str
    mode: SessionMode
    display_suffix: str | None = None
    authority_node_id: str
    access_generation: int | None = None
    session_ref: str | None = None
    session_epoch: int | None = None
    session_state: SessionState | None = None
    hard_expires_at: str | None = None


class ObserveSessionsResult(_Strict):
    ok: Literal[True]
    subject: Literal["sessions"]
    sessions: list[SessionSummary]
    next_cursor: Cursor | None


class ObserveTasksResult(_Strict):
    ok: Literal[True]
    subject: Literal["tasks"]
    summary: TaskListSummary | None = None
    tag_counts: dict[str, int] = Field(default_factory=dict)
    recommended: TaskRecommendation | None = None
    tasks: list[TaskListItem | TaskRecord] = Field(default_factory=list)
    task: TaskSnapshot | TaskRecord | None = None
    next_cursor: Cursor | None
    namespaces: list[Namespace] = Field(default_factory=list)


class ObserveNamespacesResult(_Strict):
    ok: Literal[True]
    subject: Literal["namespaces"]
    namespaces: list[Namespace]
    next_cursor: Cursor | None


ObserveSuccess = Annotated[
    ObserveSessionsResult | ObserveTasksResult | ObserveNamespacesResult,
    Field(discriminator="subject"),
]


class ObserveOutput(RootModel[ObserveSuccess | AccessError]):
    __success_type__: ClassVar[Any] = ObserveSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class MessageRecord(_Strict):
    message_hash: MessageId | None = None
    message_id: MessageId | None = None
    sender: str | None = None
    target: str | None = None
    text: str | None = None
    mode: MessageMode | None = None
    state: MessageState | None = None
    created_at: str | None = None
    timestamp: str | None = None
    acknowledged: bool | None = None
    replied: bool | None = None
    truncated: bool | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    seen_count: int | None = None
    read_at: str | None = None
    replied_at: str | None = None
    reply_message_hash: MessageId | None = None
    reply_to: MessageId | None = None
    namespace: Namespace | None = None
    task_id: TaskId | None = None
    scope: str | None = None
    delivered_to: list[str] | None = None
    message_hashes: dict[str, str] | None = None


class MessageListResult(_SessionAware):
    ok: Literal[True]
    action: Literal["inbox", "history"]
    sender: str | None = None
    messages: list[MessageRecord]
    next_cursor: Cursor | None


class MessageSendResult(_SessionAware):
    ok: Literal[True]
    action: Literal["send"]
    message: MessageRecord


class MessageAcknowledgeResult(_SessionAware):
    ok: Literal[True]
    action: Literal["acknowledge"]
    message_hash: MessageId | None = None
    state: Literal["acknowledged"]


class MessageReplyResult(_SessionAware):
    ok: Literal[True]
    action: Literal["reply"]
    message: MessageRecord


MessageSuccess = Annotated[
    MessageListResult | MessageSendResult | MessageAcknowledgeResult | MessageReplyResult,
    Field(discriminator="action"),
]


class MessageOutput(RootModel[MessageSuccess | AccessError]):
    __success_type__: ClassVar[Any] = MessageSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class TaskMutationResult(_SessionAware):
    ok: Literal[True]
    action: Literal[
        "create",
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
    task: TaskReceipt
    warnings: list[WorkflowWarning] = Field(default_factory=list)


class TaskClaimMutationResult(_SessionAware):
    ok: Literal[True]
    action: Literal["claim"]
    task: TaskSnapshot
    warnings: list[WorkflowWarning] = Field(default_factory=list)


TaskSuccess = Annotated[TaskMutationResult | TaskClaimMutationResult, Field(discriminator="action")]


class TaskOutput(RootModel[TaskSuccess | AccessError]):
    __success_type__: ClassVar[Any] = TaskSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class CommandView(_Strict):
    cmd_hash: CommandId
    status: CommandStatus
    queue_id: int | None = None
    queue_position: int | None = None
    exit_code: int | None = None
    cancelled_from: Literal["queued", "running"] | None = None
    execution_started: bool | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class CoordinationState(_Strict):
    messages: list[MessageRecord] = Field(default_factory=list)
    pending_messages: list[MessageRecord] = Field(default_factory=list)
    ack_required_pending: bool = False
    alert_pending: bool = False


class ExecutionIdentity(_Strict):
    logical_agent_id: str | None = None
    work_session_id: str | None = None
    session_epoch: int | None = None
    public_name: str | None = None
    session_ref: str | None = None
    hard_expires_at: str | None = None
    authority_node_id: str | None = None
    node_attachment_id: str | None = None


class CmdRunResult(_SessionAware):
    ok: Literal[True]
    action: Literal["run"]
    command: CommandView
    task_scope: str | None = None
    task_targets: list[str] = Field(default_factory=list)
    task_scope_options: list[str] = Field(default_factory=list)
    lines: list[str] | None = None
    overall_lines_count: int | None = None
    displayed_lines_count: int | None = None
    next_cursor: Cursor | None = None
    has_more: bool | None = None
    output_truncated: bool | None = None
    output_retained: bool | None = None
    output_pruned_at: str | None = None
    output_bytes: int | None = None
    line_truncated: bool | None = None
    coordination: CoordinationState | None = None
    identity: ExecutionIdentity | None = None


class CmdReadResult(_SessionAware):
    ok: Literal[True]
    action: Literal["read"]
    command: CommandView
    lines: list[str]
    next_offset: int | None = None
    overall_lines_count: int | None = None
    displayed_lines_count: int | None = None
    next_cursor: Cursor | None = None
    has_more: bool | None = None
    output_truncated: bool | None = None
    output_retained: bool | None = None
    output_pruned_at: str | None = None
    output_bytes: int | None = None
    line_truncated: bool | None = None
    coordination: CoordinationState | None = None
    identity: ExecutionIdentity | None = None


class CmdCancelResult(_SessionAware):
    ok: Literal[True]
    action: Literal["cancel"]
    command: CommandView
    coordination: CoordinationState | None = None
    identity: ExecutionIdentity | None = None


class CmdRecoveryResult(_SessionAware):
    ok: Literal[True]
    action: Literal["recovery"]
    command: CommandView
    lines: list[str] = Field(default_factory=list)
    overall_lines_count: int | None = None
    displayed_lines_count: int | None = None
    duration_ms: int | None = None
    output_truncated: bool | None = None
    output_retained: bool | None = None
    output_pruned_at: str | None = None
    output_bytes: int | None = None
    coordination: CoordinationState | None = None
    identity: ExecutionIdentity | None = None


CmdSuccess = Annotated[
    CmdRunResult | CmdReadResult | CmdCancelResult | CmdRecoveryResult,
    Field(discriminator="action"),
]


class CmdOutput(RootModel[CmdSuccess | AccessError]):
    __success_type__: ClassVar[Any] = CmdSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class ContextListEntry(_Strict):
    id: ContextId
    summary: str
    content: str | None = None


class ContextEntry(_Strict):
    id: ContextId
    summary: str
    content: str
    primary: bool


class ContextListResult(_Strict):
    ok: Literal[True]
    action: Literal["list"]
    primary: list[ContextListEntry]
    additional: list[ContextListEntry]
    next_cursor: Cursor | None = None


class ContextCreateResult(_SessionAware):
    ok: Literal[True]
    action: Literal["create"]
    context: ContextEntry


class ContextUpdateResult(_SessionAware):
    ok: Literal[True]
    action: Literal["update"]
    context: ContextEntry


class ContextDeleteResult(_SessionAware):
    ok: Literal[True]
    action: Literal["delete"]
    context_id: ContextId
    deleted: Literal[True]


ContextSuccess = Annotated[
    ContextListResult | ContextCreateResult | ContextUpdateResult | ContextDeleteResult,
    Field(discriminator="action"),
]


class ContextOutput(RootModel[ContextSuccess | AccessError]):
    __success_type__: ClassVar[Any] = ContextSuccess

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class QueueHealth(_Strict):
    queue_id: int
    running: str | None = None
    durable_running: str | None = None
    queued: int


class OutputCacheHealth(_Strict):
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


class TerminalHealth(_Strict):
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


class WorkflowReviewCounts(_Strict):
    BLOCKING: int | None = None
    NON_BLOCKING: int | None = None


class WorkflowHealth(_Strict):
    by_state: dict[str, int] = Field(default_factory=dict)
    by_lane: dict[str, int] = Field(default_factory=dict)
    active_claims: int | None = None
    reviews: WorkflowReviewCounts = Field(default_factory=WorkflowReviewCounts)
    unreleased_claims: int | None = None
    live_claims: int | None = None
    stale_claims: int | None = None
    ok: bool | None = None


class HealthCommandResult(_Strict):
    command: str
    lines: list[str]
    status: CommandStatus
    exit_code: int | None = None
    error: str | None = None
    ok: bool
    duration_ms: int


class HealthResult(_Strict):
    ok: Literal[True]
    application: str
    version: str
    storage: str
    auth_mode: str
    terminal: TerminalHealth
    workflow: WorkflowHealth | None = None
    custom_command: HealthCommandResult | None = None
    agent_name: str | None = None


class HealthOutput(RootModel[HealthResult | AccessError]):
    __success_type__: ClassVar[Any] = HealthResult

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _success_schema(cls.__success_type__)


class OutputContractViolation(RuntimeError):
    pass


def _known(model: type[BaseModel], raw: dict[str, Any], **overrides: Any) -> BaseModel:
    payload = {name: raw[name] for name in model.model_fields if name in raw}
    payload.update(overrides)
    return model.model_validate(payload)


def _json_payload(value: Any) -> dict[str, str]:
    return {
        "serialized": json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def _task_dependency(raw: dict[str, Any]) -> dict[str, Any]:
    return _known(TaskDependency, raw).model_dump(mode="json", exclude_unset=True)


def _task_relation(raw: dict[str, Any]) -> dict[str, Any]:
    return _known(TaskRelation, raw).model_dump(mode="json", exclude_unset=True)


def _task_output_state(raw: dict[str, Any]) -> dict[str, Any]:
    return _known(TaskOutputState, raw).model_dump(mode="json", exclude_unset=True)


def _task_event(raw: dict[str, Any]) -> dict[str, Any]:
    item = {name: raw[name] for name in TaskEvent.model_fields if name in raw and name != "payload"}
    item["payload"] = _json_payload(raw.get("payload") or {})
    return TaskEvent.model_validate(item).model_dump(mode="json", exclude_unset=True)


def _task_review(raw: dict[str, Any]) -> dict[str, Any]:
    item = {
        name: raw[name]
        for name in TaskReview.model_fields
        if name in raw and name not in {"evidence", "warnings"}
    }
    item["evidence"] = _json_payload(raw.get("evidence") or {})
    item["warnings"] = _json_payload(raw.get("warnings") or [])
    return TaskReview.model_validate(item).model_dump(mode="json", exclude_unset=True)


def _workflow_warning(raw: dict[str, Any]) -> dict[str, Any]:
    item = {
        name: raw[name]
        for name in WorkflowWarning.model_fields
        if name in raw and name != "context"
    }
    if raw.get("context"):
        item["context"] = _json_payload(raw["context"])
    return WorkflowWarning.model_validate(item).model_dump(mode="json", exclude_unset=True)


def _error_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return normalize_public_error(raw).as_dict()


def serialized_call_tool_result_size(result: CallToolResult) -> int:
    # Measure the conservative Pydantic JSON form including null-valued MCP
    # envelope fields. Actual transports may omit them, so this does not
    # underestimate the tool-result body size.
    return len(
        result.model_dump_json(
            by_alias=True,
            exclude_none=False,
        ).encode("utf-8")
    )


def _content_projection(data: dict[str, Any]) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _call_tool_result(
    data: dict[str, Any],
    *,
    content_data: dict[str, Any] | None = None,
    is_error: bool = False,
) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=_content_projection(content_data if content_data is not None else data),
            )
        ],
        structuredContent=data,
        isError=is_error,
    )


def _result(
    tool: str,
    variant: str,
    output_model: type[RootModel],
    raw: dict[str, Any],
    structured: dict[str, Any],
    *,
    compact_success_text: bool = False,
) -> CallToolResult:
    is_error = raw.get("ok") is False
    if not is_error and isinstance(raw.get("session_lifecycle"), dict):
        structured = {**structured, "session_lifecycle": raw["session_lifecycle"]}
    candidate = _error_payload(raw) if is_error else structured
    try:
        validated = output_model.model_validate(candidate)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.errors()[0].get("loc", ())) or "$"
        offending = exc.errors()[0].get("input")
        raise OutputContractViolation(
            f"output_contract_violation tool={tool} variant={variant} "
            f"path={path} offending={offending!r}: {exc}"
        ) from exc

    data = validated.model_dump(mode="json", exclude_unset=True)
    if is_error or compact_success_text:
        content_data = data
    else:
        # Preserve the legacy success text shape while bounding only the coordination
        # collections that can be repeated and arbitrarily large in application results.
        content_data = dict(raw)
        coordination = data.get("coordination")
        if isinstance(coordination, dict):
            for key in ("messages", "pending_messages"):
                if key in content_data:
                    content_data[key] = coordination.get(key, [])
    result = _call_tool_result(data, content_data=content_data, is_error=is_error)
    serialized_bytes = serialized_call_tool_result_size(result)
    if serialized_bytes <= CALL_TOOL_RESULT_BUDGET_BYTES:
        return result

    # Fail closed at the final public serialization boundary. Normal paginated
    # reads should stay below this via their lower page-data budget; this guard
    # also covers unexpectedly large coordination/error metadata.
    bounded_error = public_error("output_item_too_large").as_dict()
    try:
        error_validated = output_model.model_validate(bounded_error)
    except ValidationError as exc:
        raise OutputContractViolation(
            f"output_contract_violation tool={tool} variant={variant} "
            "could not encode bounded size error"
        ) from exc
    error_data = error_validated.model_dump(mode="json", exclude_unset=True)
    error_result = _call_tool_result(error_data, content_data=error_data, is_error=True)
    if serialized_call_tool_result_size(error_result) > CALL_TOOL_RESULT_BUDGET_BYTES:
        raise OutputContractViolation(
            f"output_contract_violation tool={tool} variant={variant} "
            "bounded size error exceeds response-size budget"
        )
    return error_result


def _message_record(raw: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    item = dict(raw)
    if "sender" not in item and "sender_name" in item:
        item["sender"] = item["sender_name"]
    if "message_hash" not in item:
        item["message_hash"] = item.get("message_ref") or item.get("message_id")
    if "message_id" not in item and item.get("message_hash"):
        item["message_id"] = item["message_hash"]
    if item.get("mode") is None:
        item["mode"] = (
            "alert" if item.get("alert") else "ack" if item.get("require_reply") else "notify"
        )
    if item.get("state") is None:
        item["state"] = (
            "replied"
            if item.get("replied_at")
            else "read"
            if item.get("read_at")
            else "seen"
            if item.get("first_seen_at")
            else "delivered"
        )
    item.update(overrides)
    return _known(MessageRecord, item).model_dump(mode="json", exclude_unset=True)


def _task_list_item(raw: dict[str, Any]) -> dict[str, Any]:
    return _known(TaskListItem, raw).model_dump(mode="json", exclude_unset=True)


def _task_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    item = {
        name: raw[name]
        for name in TaskSnapshot.model_fields
        if name in raw and name not in {"claim", "latest_checkpoint", "blocking_dependencies"}
    }
    claim = raw.get("claim")
    item["claim"] = (
        _known(TaskClaim, claim).model_dump(mode="json", exclude_unset=True)
        if isinstance(claim, dict)
        else None
    )
    checkpoint = raw.get("latest_checkpoint")
    item["latest_checkpoint"] = (
        _known(TaskCheckpointSnapshot, checkpoint).model_dump(mode="json", exclude_unset=True)
        if isinstance(checkpoint, dict)
        else None
    )
    item["blocking_dependencies"] = [
        _task_dependency(value) for value in raw.get("blocking_dependencies") or []
    ]
    return TaskSnapshot.model_validate(item).model_dump(mode="json", exclude_unset=True)


def _task_record(raw: dict[str, Any]) -> dict[str, Any]:
    item = {
        name: raw[name]
        for name in TaskRecord.model_fields
        if name in raw
        and name
        not in {
            "checkpoint",
            "result",
            "resource_context",
            "blocking_dependencies",
            "dependencies",
            "relations",
            "comments",
            "reviews",
            "events",
            "output_states",
        }
    }
    if "checkpoint" in raw:
        checkpoint = raw.get("checkpoint")
        item["checkpoint"] = (
            checkpoint if isinstance(checkpoint, str) else _json_payload(checkpoint)
        )
    if "result" in raw and raw.get("result") is not None:
        result = raw["result"]
        item["result"] = result if isinstance(result, str) else _json_payload(result)
    if isinstance(raw.get("resource_context"), dict):
        item["resource_context"] = {
            name: raw["resource_context"][name]
            for name in TaskResourceContext.model_fields
            if name in raw["resource_context"]
        }
    item["blocking_dependencies"] = [
        _task_dependency(value) for value in raw.get("blocking_dependencies") or []
    ]
    if "dependencies" in raw and raw.get("dependencies") is not None:
        item["dependencies"] = [_task_dependency(value) for value in raw["dependencies"]]
    if "relations" in raw and raw.get("relations") is not None:
        item["relations"] = [_task_relation(value) for value in raw["relations"]]
    if "output_states" in raw and raw.get("output_states") is not None:
        item["output_states"] = [_task_output_state(value) for value in raw["output_states"]]
    if "events" in raw and raw.get("events") is not None:
        item["events"] = [_task_event(value) for value in raw["events"]]
    if "comments" in raw and raw.get("comments") is not None:
        item["comments"] = [_task_event(value) for value in raw["comments"]]
    if "reviews" in raw and raw.get("reviews") is not None:
        item["reviews"] = [_task_review(value) for value in raw["reviews"]]
    return TaskRecord.model_validate(item).model_dump(mode="json", exclude_unset=True)


def session_result(raw: dict[str, Any], action: str) -> CallToolResult:
    if not raw.get("ok"):
        return _result("session", action, SessionOutput, raw, {})
    if action == "start":
        session = {
            key: raw[key] for key in SessionInfo.model_fields if key in raw and raw[key] is not None
        }
        session["session_state"] = raw.get("session_state") or "active"
        structured = {"ok": True, "action": "start", "session": session}
        if raw.get("access_code") is not None:
            structured["access_code"] = raw["access_code"]
    else:
        structured = {
            "ok": True,
            "action": action,
            "session_state": "interrupted" if action == "interrupt" else "inactive",
        }
        for key in ("session_ref", "public_name", "mode", "stopping"):
            if key in raw and raw[key] is not None:
                structured[key] = raw[key]
    return _result("session", action, SessionOutput, raw, structured)


def observe_result(
    raw: dict[str, Any],
    subject: str,
    *,
    detail: str = "summary",
    task_id: str | None = None,
) -> CallToolResult:
    if not raw.get("ok"):
        return _result("observe", subject, ObserveOutput, raw, {})
    if subject == "sessions":
        sessions = [
            _known(SessionSummary, item).model_dump(mode="json", exclude_unset=True)
            for item in raw.get("sessions", [])
        ]
        next_cursor = raw.get("next_cursor")
        structured = {
            "ok": True,
            "subject": "sessions",
            "sessions": sessions,
            "next_cursor": None if next_cursor is None else str(next_cursor),
        }
    elif subject == "namespaces":
        next_cursor = raw.get("next_cursor")
        structured = {
            "ok": True,
            "subject": "namespaces",
            "namespaces": raw.get("namespaces") or [],
            "next_cursor": None if next_cursor is None else str(next_cursor),
        }
    else:
        summary = None
        if isinstance(raw.get("summary"), dict):
            summary = _known(TaskListSummary, raw["summary"]).model_dump(
                mode="json", exclude_unset=True
            )
        recommended = None
        if isinstance(raw.get("recommended"), dict):
            recommended = _known(TaskRecommendation, raw["recommended"]).model_dump(
                mode="json", exclude_unset=True
            )
        next_cursor = raw.get("next_cursor")
        structured = {
            "ok": True,
            "subject": "tasks",
            "tasks": [
                _task_list_item(item) if detail == "summary" else _task_record(item)
                for item in raw.get("tasks", [])
            ],
            "tag_counts": raw.get("tag_counts") or {},
            "next_cursor": None if next_cursor is None else str(next_cursor),
            "namespaces": raw.get("namespaces") or [],
        }
        if isinstance(raw.get("task"), dict):
            structured["task"] = (
                _task_snapshot(raw["task"])
                if detail == "summary" and task_id is not None
                else _task_record(raw["task"])
            )
        if summary is not None:
            structured["summary"] = summary
        if recommended is not None:
            structured["recommended"] = recommended
    return _result("observe", subject, ObserveOutput, raw, structured)


def message_result(
    raw: dict[str, Any],
    *,
    sender: str,
    text: str | None,
    target: str | None,
    message_hash: str | None,
    mode: str | None,
    require_reply: bool,
    alert: bool,
    show_all: bool,
) -> CallToolResult:
    if not raw.get("ok"):
        variant = (
            "reply"
            if message_hash is not None and text is not None
            else "acknowledge"
            if message_hash is not None
            else "send"
            if text is not None
            else "history"
            if show_all
            else "inbox"
        )
        return _result("message", variant, MessageOutput, raw, {})
    if message_hash is not None and text is not None:
        action = "reply"
        record = {
            "message_hash": raw.get("message_hash") or message_hash,
            "reply_to": raw.get("reply_to") or message_hash,
            "sender": raw.get("sender") or sender,
            "state": "replied",
        }
        structured = {"ok": True, "action": action, "message": record}
    elif message_hash is not None:
        action = "acknowledge"
        structured = {
            "ok": True,
            "action": action,
            "message_hash": raw.get("message_hash") or message_hash,
            "state": "acknowledged",
        }
    elif text is not None:
        action = "send"
        normalized_mode = mode or ("alert" if alert or require_reply else "notify")
        record = {
            "message_hash": raw.get("message_hash"),
            "message_hashes": raw.get("message_hashes"),
            "sender": raw.get("sender") or sender,
            "target": target,
            "mode": normalized_mode,
            "state": "delivered",
            "scope": raw.get("scope"),
            "delivered_to": raw.get("delivered_to"),
            "namespace": raw.get("namespace"),
            "task_id": raw.get("task_id"),
        }
        record = {k: v for k, v in record.items() if v is not None}
        structured = {
            "ok": True,
            "action": action,
            "message": _known(MessageRecord, record).model_dump(mode="json", exclude_unset=True),
        }
    else:
        action = "history" if show_all else "inbox"
        items = raw.get("messages")
        if items is None:
            items = raw.get("inbox") or []
        next_cursor = raw.get("next_cursor")
        structured = {
            "ok": True,
            "action": action,
            "sender": raw.get("sender") or sender,
            "messages": [_message_record(item) for item in items],
            "next_cursor": None if next_cursor is None else str(next_cursor),
        }
    return _result("message", action, MessageOutput, raw, structured)


def task_result(raw: dict[str, Any], action: str) -> CallToolResult:
    if not raw.get("ok"):
        return _result("task", action, TaskOutput, raw, {})
    warnings = [_workflow_warning(item) for item in raw.get("warnings", [])]
    if action == "claim":
        task = _task_snapshot(raw.get("task") or {})
    else:
        record = TaskRecord.model_validate(_task_record(raw.get("task") or {}))
        task = project_task_receipt(
            record,
            include_description=action in {"create", "update"},
            include_checkpoint=action in {"create", "update", "checkpoint"},
            include_result=action in {"create", "update", "done", "state"},
        ).model_dump(mode="json", exclude_none=True)
    structured = {
        "ok": True,
        "action": action,
        "task": task,
        "warnings": warnings,
    }
    return _result("task", action, TaskOutput, raw, structured, compact_success_text=True)


def _compact_coordination_messages(
    items: list[dict[str, Any]], *, seen: set[str] | None = None
) -> tuple[list[dict[str, Any]], set[str]]:
    seen_ids = set() if seen is None else set(seen)
    compact: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        normalized = _message_record(raw_item)
        message_id = str(normalized.get("message_hash") or normalized.get("message_id") or "")
        if message_id and message_id in seen_ids:
            continue
        if len(compact) >= MAX_COORDINATION_MESSAGES:
            continue
        compact.append(_message_record(summary_message(normalized)))
        if message_id:
            seen_ids.add(message_id)
    return compact, seen_ids


def _coordination(raw: dict[str, Any]) -> dict[str, Any] | None:
    keys = ("messages", "pending_messages", "ack_required_pending", "alert_pending")
    if not any(key in raw for key in keys):
        return None
    messages, seen = _compact_coordination_messages(raw.get("messages", []))
    pending, _ = _compact_coordination_messages(raw.get("pending_messages", []), seen=seen)
    return {
        "messages": messages,
        "pending_messages": pending,
        "ack_required_pending": bool(raw.get("ack_required_pending")),
        "alert_pending": bool(raw.get("alert_pending")),
    }


def _identity(raw: dict[str, Any]) -> dict[str, Any] | None:
    payload = {
        key: raw[key]
        for key in ExecutionIdentity.model_fields
        if key in raw and raw[key] is not None
    }
    return payload or None


def _command(raw: dict[str, Any], *, action: str) -> dict[str, Any]:
    status = raw.get("status")
    if status is None:
        if action == "run":
            status = "running" if raw.get("execution_started") else "queued"
        elif action == "cancel":
            status = "cancelled"
        else:
            exit_code = raw.get("exit_code")
            status = (
                "running"
                if exit_code is None and action == "read"
                else "completed"
                if exit_code in (None, 0)
                else "failed"
            )
    payload = {
        "cmd_hash": raw.get("cmd_hash"),
        "status": status,
    }
    for key in (
        "queue_id",
        "queue_position",
        "exit_code",
        "cancelled_from",
        "execution_started",
        "claimed_at",
        "started_at",
        "finished_at",
    ):
        if key in raw:
            payload[key] = raw[key]
    return payload


def cmd_result(raw: dict[str, Any], action: str) -> CallToolResult:
    if not raw.get("ok"):
        return _result("cmd", action, CmdOutput, raw, {})
    structured: dict[str, Any] = {
        "ok": True,
        "action": action,
        "command": _command(raw, action=action),
    }
    coord = _coordination(raw)
    ident = _identity(raw)
    if coord is not None:
        structured["coordination"] = coord
    if ident is not None:
        structured["identity"] = ident
    if action == "run":
        for key in (
            "task_scope",
            "task_targets",
            "task_scope_options",
            "lines",
            "overall_lines_count",
            "displayed_lines_count",
            "next_cursor",
            "has_more",
            "output_truncated",
            "output_retained",
            "output_pruned_at",
            "output_bytes",
            "line_truncated",
        ):
            if key in raw and raw[key] is not None:
                structured[key] = raw[key]
    elif action == "read":
        structured["lines"] = raw.get("lines") or []
        for key in (
            "next_offset",
            "overall_lines_count",
            "displayed_lines_count",
            "has_more",
            "output_truncated",
            "output_retained",
            "output_pruned_at",
            "output_bytes",
            "line_truncated",
        ):
            if key in raw:
                structured[key] = raw[key]
        if "next_cursor" in raw:
            structured["next_cursor"] = (
                None if raw["next_cursor"] is None else str(raw["next_cursor"])
            )
    elif action == "recovery":
        structured["lines"] = raw.get("lines") or []
        for key in (
            "overall_lines_count",
            "displayed_lines_count",
            "duration_ms",
            "output_truncated",
            "output_retained",
            "output_pruned_at",
            "output_bytes",
        ):
            if key in raw:
                structured[key] = raw[key]
    return _result("cmd", action, CmdOutput, raw, structured)


def context_result(raw: dict[str, Any], action: str) -> CallToolResult:
    if not raw.get("ok"):
        return _result("context", action, ContextOutput, raw, {})
    if action == "list":
        structured = {
            "ok": True,
            "action": "list",
            "primary": [
                _known(ContextListEntry, item).model_dump(mode="json", exclude_unset=True)
                for item in raw.get("primary", [])
            ],
            "additional": [
                _known(ContextListEntry, item).model_dump(mode="json", exclude_unset=True)
                for item in raw.get("additional", [])
            ],
        }
        if "next_cursor" in raw:
            structured["next_cursor"] = (
                None if raw["next_cursor"] is None else str(raw["next_cursor"])
            )
    elif action in {"create", "update"}:
        structured = {
            "ok": True,
            "action": action,
            "context": _known(
                ContextEntry, raw.get("entry") or raw.get("context") or {}
            ).model_dump(mode="json", exclude_unset=True),
        }
    else:
        structured = {
            "ok": True,
            "action": "delete",
            "context_id": raw.get("deleted_id") or raw.get("context_id"),
            "deleted": True,
        }
    return _result("context", action, ContextOutput, raw, structured)


def health_result(raw: dict[str, Any]) -> CallToolResult:
    if not raw.get("ok"):
        return _result("health", "health", HealthOutput, raw, {})
    terminal = _known(TerminalHealth, raw.get("terminal") or {}).model_dump(
        mode="json", exclude_unset=True
    )
    structured: dict[str, Any] = {
        "ok": True,
        "application": raw.get("application"),
        "version": raw.get("version"),
        "storage": raw.get("storage"),
        "auth_mode": raw.get("auth_mode"),
        "terminal": terminal,
    }
    if isinstance(raw.get("workflow"), dict):
        structured["workflow"] = _known(WorkflowHealth, raw["workflow"]).model_dump(
            mode="json", exclude_unset=True
        )
    if isinstance(raw.get("custom_command"), dict):
        structured["custom_command"] = _known(
            HealthCommandResult, raw["custom_command"]
        ).model_dump(mode="json", exclude_unset=True)
    if raw.get("agent_name") is not None:
        structured["agent_name"] = raw["agent_name"]
    return _result("health", "health", HealthOutput, raw, structured)


def install_public_output_contract(
    mcp, tool_name: str, output_model: type[RootModel], canonicalizer
) -> None:
    tool = {item.name: item for item in mcp._tool_manager.list_tools()}[tool_name]
    raw_fn = tool.fn

    async def contracted(**kwargs):
        raw = await raw_fn(**kwargs)
        return canonicalizer(raw, kwargs)

    tool.fn = contracted
    tool.fn_metadata.output_model = output_model
    tool.fn_metadata.output_schema = output_model.success_schema()
    tool.fn_metadata.wrap_output = False

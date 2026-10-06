"""Transport-independent task projections shared by application and compatibility adapters.

TaskRecord is the legacy expanded projection. New consumers select TaskDetail
(current state), TaskHistory (bounded pages), TaskWorkingSet or TaskReceipt.
The models preserve authoritative state; projection helpers do not recompute it.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from itertools import islice
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Semantic DTO budget, independent of any transport envelope. Leave room below
# the public 64-KiB ceiling for action/cursor framing. Runtime paginators may
# apply smaller budgets, e.g. when supporting legacy mirrored text.
MAX_TASK_PROJECTION_BYTES = 60 * 1024


class _BoundedProjection(_Strict):
    @model_validator(mode="after")
    def _validate_projection_budget(self) -> Self:
        if len(self.model_dump_json(exclude_none=True).encode("utf-8")) > MAX_TASK_PROJECTION_BYTES:
            raise ValueError("task projection exceeds its serialized-byte budget")
        return self


class TaskId(RootModel[str]):
    root: Annotated[str, Field(min_length=1)]


class Namespace(RootModel[str]):
    root: Annotated[str, Field(min_length=1)]


class Cursor(RootModel[str]):
    root: str


class TaskState(StrEnum):
    ready = "ready"
    in_progress = "in_progress"
    blocked = "blocked"
    deferred = "deferred"
    done = "done"


class TaskOperationalStatus(StrEnum):
    ready = "ready"
    in_progress = "in_progress"
    blocked = "blocked"
    deferred = "deferred"
    done = "done"


class TaskLane(StrEnum):
    implementation = "implementation"
    review = "review"
    release = "release"
    integration = "integration"
    general = "general"


class TaskPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class JsonPayload(_Strict):
    """Explicit extension boundary for backend/domain JSON payloads."""

    serialized: str


class TaskResourceContext(_Strict):
    repo: str | None = None
    path: str | None = None
    scope: str | None = None


class TaskDependency(_Strict):
    namespace: Namespace
    task_id: TaskId
    state: TaskState | Literal["missing"]
    archived: bool
    satisfied: bool


class TaskRelation(_Strict):
    direction: Literal["incoming", "outgoing"]
    kind: str
    namespace: Namespace
    task_id: TaskId
    created_at: str
    created_by: str


class TaskOutputState(_Strict):
    output_state_id: int
    output_refs: list[str] = Field(default_factory=list)
    created_at: str


class TaskEvent(_Strict):
    id: int
    event_type: str
    payload: JsonPayload
    created_at: str
    logical_agent_id: str | None = None
    work_session_id: str | None = None
    session_epoch: int | None = None
    agent_name: str | None = None


class TaskReview(_Strict):
    output_state_id: int | None = None
    output_refs: list[str] = Field(default_factory=list)
    dimension: Literal["A", "C", "R"]
    verdict: Literal["NON_BLOCKING", "BLOCKING"]
    evidence: JsonPayload
    warnings: JsonPayload
    reviewed_at: str
    reviewer: str | None = None
    agent_name: str | None = None


class TaskClaim(_Strict):
    agent_name: str
    claimed_at: str
    claim_age_seconds: int
    claim_intent: str
    role: Literal["owner", "participant"]


class TaskCheckpointSnapshot(_Strict):
    text: Annotated[str, Field(max_length=4000)]
    author: str
    created_at: str
    revision: Annotated[int, Field(ge=1)]


class TaskListItem(_Strict):
    namespace: Namespace
    task_id: TaskId
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus
    revision: int
    claimed_by: str | None
    blocking_count: Annotated[int, Field(ge=0)]
    has_checkpoint: bool


class TaskSnapshot(_Strict):
    namespace: Namespace
    task_id: TaskId
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus
    revision: int
    claim: TaskClaim | None
    next_action: str
    description_preview: Annotated[str, Field(max_length=1500)]
    description_truncated: bool
    latest_checkpoint: TaskCheckpointSnapshot | None
    blocking_dependencies: list[TaskDependency] = Field(default_factory=list)


class WorkflowWarning(_Strict):
    code: str
    severity: str = "warning"
    message: str
    task_id: str | None = None
    context: JsonPayload | None = None


class TaskDetail(_BoundedProjection):
    namespace: Namespace
    task_id: TaskId
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus
    revision: int
    next_action: str = ""
    cooperative: bool = False
    checkpoint: str | JsonPayload | None = None
    candidate_ref: str | None = None
    result: str | JsonPayload | None = None
    tags: list[str] = Field(default_factory=list)
    isolation_hint: str = "none"
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    output_state_id: int | None = None
    review_requirements: list[Literal["A", "C", "R"]] = Field(default_factory=list)
    claims: list[TaskClaim] = Field(default_factory=list)
    owner: TaskClaim | str | None = None
    participants: list[TaskClaim] = Field(default_factory=list)
    active: bool = False
    blocking_dependencies: list[TaskDependency] = Field(default_factory=list)
    state_changed_at: str | None = None
    ready_since: str | None = None
    archived_at: str | None = None
    archive_note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    description: str | None = None
    resource_context: TaskResourceContext | None = None
    dependencies: list[TaskDependency] | None = None
    relations: list[TaskRelation] | None = None


class TaskRecord(_Strict):
    namespace: Namespace
    task_id: TaskId
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus
    revision: int
    next_action: str = ""
    cooperative: bool = False
    checkpoint: str | JsonPayload | None = None
    candidate_ref: str | None = None
    result: str | JsonPayload | None = None
    tags: list[str] = Field(default_factory=list)
    isolation_hint: str = "none"
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    output_state_id: int | None = None
    output_states: list[TaskOutputState] | None = None
    review_requirements: list[Literal["A", "C", "R"]] = Field(default_factory=list)
    claims: list[TaskClaim] = Field(default_factory=list)
    owner: TaskClaim | str | None = None
    participants: list[TaskClaim] = Field(default_factory=list)
    active: bool = False
    blocking_dependencies: list[TaskDependency] = Field(default_factory=list)
    state_changed_at: str | None = None
    ready_since: str | None = None
    archived_at: str | None = None
    archive_note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    description: str | None = None
    resource_context: TaskResourceContext | None = None
    dependencies: list[TaskDependency] | None = None
    relations: list[TaskRelation] | None = None
    comments: list[TaskEvent] | None = None
    reviews: list[TaskReview] | None = None
    events: list[TaskEvent] | None = None


class TaskRecommendation(_Strict):
    namespace: Namespace
    task_id: TaskId
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    operational_status: TaskOperationalStatus
    tags: list[str] = Field(default_factory=list)
    ready_since: str | None = None


class TaskListSummary(_Strict):
    visible: int | None = None
    returned: int | None = None
    by_lane: dict[str, int] | None = None
    by_state: dict[str, int] | None = None
    by_operational_status: dict[str, int] | None = None
    pressure: dict[str, int] | None = None
    tag_counts: dict[str, int] | None = None
    claimable_count: int | None = None
    oldest_claimable_ready_since: str | None = None
    oldest_claimable_ready_age_seconds: int | None = None
    missing_dependency_count: int | None = None


class TaskReceipt(_BoundedProjection):
    """Mutation outcome without current-state bodies or historical collections."""

    namespace: Namespace
    task_id: TaskId
    revision: Annotated[int, Field(ge=1)]
    state: TaskState
    operational_status: TaskOperationalStatus
    owner: str | None = None
    output_state_id: Annotated[int, Field(ge=1)] | None = None
    archived: Literal[True] | None = None
    warnings: list[WorkflowWarning] | None = Field(default=None, max_length=100)


class TaskWorkingSet(TaskSnapshot, _BoundedProjection):
    """Current working snapshot plus bounded newest-first collaboration context."""

    recent_comments: list[TaskEvent] = Field(default_factory=list, max_length=10)
    recent_checkpoints: list[TaskCheckpointSnapshot] = Field(default_factory=list, max_length=10)


class _TaskHistoryPage(_BoundedProjection):
    namespace: Namespace
    task_id: TaskId
    next_cursor: Cursor | None = None


class TaskEventHistory(_TaskHistoryPage):
    kind: Literal["comments", "checkpoints", "events"]
    items: list[TaskEvent] = Field(default_factory=list, max_length=100)


class TaskReviewHistory(_TaskHistoryPage):
    kind: Literal["reviews"]
    items: list[TaskReview] = Field(default_factory=list, max_length=100)


class TaskOutputHistory(_TaskHistoryPage):
    kind: Literal["output_states"]
    items: list[TaskOutputState] = Field(default_factory=list, max_length=100)


class TaskHistory(
    RootModel[
        Annotated[
            TaskEventHistory | TaskReviewHistory | TaskOutputHistory,
            Field(discriminator="kind"),
        ]
    ]
):
    """A single explicitly selected, bounded history stream with an opaque cursor."""


def project_task_detail(record: TaskRecord) -> TaskDetail:
    """Select current fields only; never traverse or serialize legacy histories."""
    return TaskDetail.model_validate(
        {
            name: getattr(record, name)
            for name in TaskDetail.model_fields
            if name in record.model_fields_set
        }
    )


def project_task_receipt(
    record: TaskRecord,
    *,
    output_state_changed: bool = False,
    warnings: Iterable[WorkflowWarning] = (),
) -> TaskReceipt:
    """Project an authoritative mutation result, preserving its revision/state."""
    data = {
        "namespace": record.namespace,
        "task_id": record.task_id,
        "revision": record.revision,
        "state": record.state,
        "operational_status": record.operational_status,
    }
    owner = record.owner
    if owner is not None:
        data["owner"] = owner.agent_name if isinstance(owner, TaskClaim) else owner
    if output_state_changed and record.output_state_id is not None:
        data["output_state_id"] = record.output_state_id
    if record.archived_at is not None:
        data["archived"] = True
    # Validate overflow rather than silently dropping an outcome-relevant warning.
    selected_warnings = list(islice(warnings, 101))
    if selected_warnings:
        data["warnings"] = selected_warnings
    return TaskReceipt.model_validate(data)


def _working_set_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
        raise ValueError("working-set limits must be integers between 0 and 10")
    return value


def project_task_working_set(
    snapshot: TaskSnapshot,
    *,
    comments: Iterable[TaskEvent] = (),
    checkpoints: Iterable[TaskCheckpointSnapshot] = (),
    comments_limit: int = 1,
    checkpoints_limit: int = 1,
) -> TaskWorkingSet:
    """Take only requested newest-first items; zero limits never touch iterators."""
    comments_limit = _working_set_limit(comments_limit)
    checkpoints_limit = _working_set_limit(checkpoints_limit)
    data = {name: getattr(snapshot, name) for name in TaskSnapshot.model_fields}
    data["recent_comments"] = list(islice(comments, comments_limit)) if comments_limit else []
    data["recent_checkpoints"] = (
        list(islice(checkpoints, checkpoints_limit)) if checkpoints_limit else []
    )
    return TaskWorkingSet.model_validate(data)

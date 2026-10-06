"""Canonical task inputs shared by all application adapters."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from terminal_mcp.api_models import (
    ReviewDimension,
    ReviewVerdict,
    TaskLane,
    TaskPriority,
    TaskState,
)
from terminal_mcp.application.input_limits import (
    MAX_SQLITE_INTEGER,
    TASK_CHECKPOINT_MAX_BYTES,
    TASK_RESOURCE_CONTEXT_MAX_BYTES,
    TASK_RESULT_MAX_BYTES,
    TASK_RESULT_TEXT_MAX_CHARS,
    TASK_REVIEW_EVIDENCE_MAX_BYTES,
    require_serialized_json_limit,
)

AccessCode = Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
Namespace = Annotated[str, Field(min_length=1, max_length=120)]
TaskId = Annotated[str, Field(min_length=1, max_length=120)]
TaskRef = Annotated[str, Field(min_length=1, max_length=512)]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
ExpectedRevision = Annotated[int, Field(ge=1, le=MAX_SQLITE_INTEGER)]
ResultText = Annotated[str, Field(max_length=TASK_RESULT_TEXT_MAX_CHARS)]
ResultValue = Annotated[
    ResultText | dict[str, object] | list[object],
    WithJsonSchema(
        {
            "oneOf": [
                {"type": "string", "maxLength": TASK_RESULT_TEXT_MAX_CHARS},
                {"type": "object"},
                {"type": "array"},
            ],
            "x-maxSerializedBytes": TASK_RESULT_MAX_BYTES,
        }
    ),
]
ResourceContext = Annotated[
    dict[str, object],
    WithJsonSchema({"type": "object", "x-maxSerializedBytes": TASK_RESOURCE_CONTEXT_MAX_BYTES}),
]
ReviewEvidence = Annotated[
    dict[str, object],
    WithJsonSchema({"type": "object", "x-maxSerializedBytes": TASK_REVIEW_EVIDENCE_MAX_BYTES}),
]
CheckpointText = Annotated[str, Field(max_length=4000)]
CheckpointValue = Annotated[
    CheckpointText | dict[str, object] | list[object],
    WithJsonSchema(
        {
            "oneOf": [
                {"type": "string", "maxLength": 4000},
                {"type": "object"},
                {"type": "array"},
            ],
            "x-maxSerializedBytes": TASK_CHECKPOINT_MAX_BYTES,
        }
    ),
]
InputRefs = Annotated[list[TaskRef], Field(max_length=64)]
OutputRefs = Annotated[list[TaskRef], Field(max_length=64)]
Tags = Annotated[list[Tag], Field(max_length=50)]


class StrictTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator(
        "resource_context", "checkpoint", "result", "evidence", mode="after", check_fields=False
    )
    @classmethod
    def bounded_extension_json(cls, value: object, info: ValidationInfo):
        if value is None:
            return value
        limits = {
            "resource_context": TASK_RESOURCE_CONTEXT_MAX_BYTES,
            "checkpoint": TASK_CHECKPOINT_MAX_BYTES,
            "result": TASK_RESULT_MAX_BYTES,
            "evidence": TASK_REVIEW_EVIDENCE_MAX_BYTES,
        }
        return require_serialized_json_limit(
            value, field=info.field_name, limit=limits[info.field_name]
        )


class TaskDependency(StrictTaskModel):
    task_id: TaskId
    namespace: Namespace | None = None


TaskDependencies = Annotated[list[TaskDependency], Field(max_length=100)]


class TaskIdentityRequest(StrictTaskModel):
    code: AccessCode
    namespace: Namespace
    task_id: TaskId


class TaskRevisionRequest(TaskIdentityRequest):
    expected_revision: ExpectedRevision | None = None


class TaskCreateRequest(StrictTaskModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "done"}}, "required": ["state"]},
                    "then": {
                        "required": ["result"],
                        "properties": {"result": {"not": {"type": "null"}}},
                    },
                }
            ]
        },
    )

    action: Literal["create"]
    code: AccessCode
    namespace: Namespace
    task_id: TaskId | None = None
    isolation_hint: Annotated[str, Field(min_length=1, max_length=160)]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    lane: TaskLane = "general"
    priority: TaskPriority = "P2"
    state: TaskState = "ready"
    description: Annotated[str, Field(max_length=8000)] | None = None
    next_action: Annotated[str, Field(max_length=2000)] | None = None
    resource_context: ResourceContext | None = None
    cooperative: bool = False
    checkpoint: CheckpointValue | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None
    input_refs: InputRefs | None = None
    output_refs: OutputRefs | None = None
    result: ResultValue | None = None
    tags: Tags | None = None
    dependencies: TaskDependencies | None = None
    force: bool = False
    force_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_result_for_done(self):
        if self.state == "done" and self.result is None:
            raise ValueError("result is required when state=done")
        return self


class TaskClaimRequest(TaskRevisionRequest):
    action: Literal["claim"]
    claim_intent: Annotated[str, Field(min_length=1, max_length=160)]
    force: bool = False
    force_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class TaskReleaseRequest(TaskRevisionRequest):
    action: Literal["release"]
    release_reason: Annotated[str, Field(min_length=1, max_length=4000)] | None = Field(
        default=None,
        description=(
            "Required only when releasing an existing live claim; omitted for the idempotent "
            "not-claimed path, which TaskCoordinator resolves from current claim state."
        ),
    )


class TaskUpdateRequest(TaskRevisionRequest):
    action: Literal["update"]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    lane: TaskLane | None = None
    priority: TaskPriority | None = None
    state: TaskState | None = None
    description: Annotated[str, Field(max_length=8000)] | None = None
    next_action: Annotated[str, Field(max_length=2000)] | None = None
    resource_context: ResourceContext | None = None
    cooperative: bool | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None
    input_refs: InputRefs | None = None
    output_refs: OutputRefs | None = None
    tags: Tags | None = None
    dependencies: TaskDependencies | None = None
    checkpoint: CheckpointValue | None = None
    result: ResultValue | None = None
    blocker_reason: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    force: bool = False
    force_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class TaskCheckpointRequest(TaskRevisionRequest):
    action: Literal["checkpoint"]
    checkpoint: CheckpointValue


class TaskCommentRequest(TaskIdentityRequest):
    action: Literal["comment"]
    comment_text: Annotated[str, Field(min_length=1, max_length=4000)]


class TaskRelationRequest(TaskRevisionRequest):
    relation_kind: Annotated[str, Field(min_length=1, max_length=64)]
    related_namespace: Namespace | None = None
    related_task_id: TaskId


class TaskRelateRequest(TaskRelationRequest):
    action: Literal["relate"]


class TaskUnrelateRequest(TaskRelationRequest):
    action: Literal["unrelate"]


class TaskStateRequest(TaskRevisionRequest):
    action: Literal["state"]
    state: TaskState
    blocker_reason: Annotated[str, Field(min_length=1, max_length=4000)] | None = Field(
        default=None,
        description=(
            "Required by TaskCoordinator only for a real transition into blocked while a live "
            "claim exists; optional for idempotent blocked state calls."
        ),
    )
    result: ResultValue | None = Field(
        default=None,
        description=(
            "Required by TaskCoordinator only for a real transition into done; optional for "
            "idempotent already-done state calls."
        ),
    )
    force: bool = False
    force_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class TaskDoneRequest(TaskRevisionRequest):
    action: Literal["done"]
    result: ResultValue | None = Field(
        default=None,
        description=(
            "Required for normal completion. It may be omitted only for the existing idempotent "
            "already-done compatibility path; TaskCoordinator remains authoritative for that "
            "state check."
        ),
    )
    output_refs: OutputRefs | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None
    force: bool = False
    force_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class TaskArchiveRequest(TaskRevisionRequest):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "anyOf": [
                {
                    "required": ["archive_note"],
                    "properties": {"archive_note": {"not": {"type": "null"}}},
                },
                {
                    "required": ["note"],
                    "properties": {"note": {"not": {"type": "null"}}},
                },
            ]
        },
    )

    action: Literal["archive"]
    archive_note: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    note: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_archive_note(self):
        if self.archive_note is None and self.note is None:
            raise ValueError("archive_note or note is required")
        return self


class TaskReviewRequest(TaskRevisionRequest):
    action: Literal["review"]
    dimensions: Annotated[
        list[ReviewDimension],
        Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}),
    ]
    verdict: ReviewVerdict
    evidence: ReviewEvidence | None = None

    @field_validator("dimensions")
    @classmethod
    def dimensions_are_unique(cls, value: list[ReviewDimension]):
        if len(value) != len(set(value)):
            raise ValueError("dimensions must be unique")
        return value


class NamespaceCreateRequest(StrictTaskModel):
    action: Literal["namespace_create"]
    code: AccessCode
    namespace: Namespace
    priority: TaskPriority = "P2"


class NamespaceRevisionRequest(StrictTaskModel):
    code: AccessCode
    namespace: Namespace
    expected_revision: ExpectedRevision | None = None


class NamespaceUpdateRequest(NamespaceRevisionRequest):
    action: Literal["namespace_update"]
    priority: TaskPriority


class NamespaceArchiveRequest(NamespaceRevisionRequest):
    action: Literal["namespace_archive"]
    archive_note: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    note: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_archive_note(self):
        if self.archive_note is None and self.note is None:
            raise ValueError("archive_note or note is required")
        return self


class NamespaceRestoreRequest(NamespaceRevisionRequest):
    action: Literal["namespace_restore"]


TaskRequest = Annotated[
    TaskCreateRequest
    | TaskClaimRequest
    | TaskReleaseRequest
    | TaskUpdateRequest
    | TaskCheckpointRequest
    | TaskCommentRequest
    | TaskRelateRequest
    | TaskUnrelateRequest
    | TaskStateRequest
    | TaskDoneRequest
    | TaskArchiveRequest
    | TaskReviewRequest
    | NamespaceCreateRequest
    | NamespaceUpdateRequest
    | NamespaceArchiveRequest
    | NamespaceRestoreRequest,
    Field(discriminator="action"),
]

TASK_ACTIONS = {
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
    "namespace_create",
    "namespace_update",
    "namespace_archive",
    "namespace_restore",
}


def task_request_to_backend(request: TaskRequest) -> tuple[str, str, str | None, dict[str, object]]:
    data = request.model_dump(exclude_none=True)
    action = data.pop("action")
    code = data.pop("code")
    namespace = data.pop("namespace")
    task_id = data.pop("task_id", None)
    return code, namespace, task_id, {"action": action, **data}

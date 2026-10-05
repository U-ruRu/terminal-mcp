from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from terminal_mcp.api_models import (
    ReviewDimension,
    ReviewVerdict,
    TaskLane,
    TaskPriority,
    TaskState,
)

AccessCode = Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
Namespace = Annotated[str, Field(min_length=1, max_length=120)]
TaskId = Annotated[str, Field(min_length=1, max_length=120)]
TaskRef = Annotated[str, Field(min_length=1, max_length=512)]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
ExpectedRevision = Annotated[int, Field(ge=1)]
ResultValue = str | dict[str, object] | list[object]
CheckpointValue = str | dict[str, object] | list[object]
InputRefs = Annotated[list[TaskRef], Field(max_length=64)]
OutputRefs = Annotated[list[TaskRef], Field(max_length=64)]
Tags = Annotated[list[Tag], Field(max_length=50)]


class StrictTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
                    "then": {"required": ["result"]},
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
    resource_context: dict[str, object] | None = None
    cooperative: bool = False
    checkpoint: CheckpointValue | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None
    input_refs: InputRefs | None = None
    output_refs: OutputRefs | None = None
    result: ResultValue | None = None
    tags: Tags | None = None
    dependencies: TaskDependencies | None = None

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
    release_reason: Annotated[str, Field(min_length=1, max_length=4000)]


class TaskUpdateRequest(TaskRevisionRequest):
    action: Literal["update"]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    lane: TaskLane | None = None
    priority: TaskPriority | None = None
    state: TaskState | None = None
    description: Annotated[str, Field(max_length=8000)] | None = None
    next_action: Annotated[str, Field(max_length=2000)] | None = None
    resource_context: dict[str, object] | None = None
    cooperative: bool | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None
    input_refs: InputRefs | None = None
    output_refs: OutputRefs | None = None
    tags: Tags | None = None
    dependencies: TaskDependencies | None = None


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
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "blocked"}}, "required": ["state"]},
                    "then": {"required": ["blocker_reason"]},
                },
                {
                    "if": {"properties": {"state": {"const": "done"}}, "required": ["state"]},
                    "then": {"required": ["result"]},
                },
            ]
        },
    )

    action: Literal["state"]
    state: TaskState
    blocker_reason: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    result: ResultValue | None = None

    @model_validator(mode="after")
    def require_state_context(self):
        if self.state == "blocked" and self.blocker_reason is None:
            raise ValueError("blocker_reason is required when state=blocked")
        if self.state == "done" and self.result is None:
            raise ValueError("result is required when state=done")
        return self


class TaskDoneRequest(TaskRevisionRequest):
    action: Literal["done"]
    result: ResultValue
    output_refs: OutputRefs | None = None
    candidate_ref: Annotated[str, Field(max_length=200)] | None = None


class TaskArchiveRequest(TaskRevisionRequest):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={"anyOf": [{"required": ["archive_note"]}, {"required": ["note"]}]},
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
    evidence: dict[str, object] | None = None

    @field_validator("dimensions")
    @classmethod
    def dimensions_are_unique(cls, value: list[ReviewDimension]):
        if len(value) != len(set(value)):
            raise ValueError("dimensions must be unique")
        return value


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
    | TaskReviewRequest,
    Field(discriminator="action"),
]


def task_request_to_backend(request: TaskRequest) -> tuple[str, str, str | None, dict[str, object]]:
    data = request.model_dump(exclude_none=True)
    action = data.pop("action")
    code = data.pop("code")
    namespace = data.pop("namespace")
    task_id = data.pop("task_id", None)
    return code, namespace, task_id, {"action": action, **data}

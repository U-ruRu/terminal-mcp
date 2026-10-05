from typing import Annotated, Literal

from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
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

AccessCode = Annotated[str, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")]
Namespace = Annotated[str, Field(min_length=1, max_length=120)]
TaskId = Annotated[str, Field(min_length=1, max_length=120)]
TaskRef = Annotated[str, Field(min_length=1, max_length=512)]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
ExpectedRevision = Annotated[int, Field(ge=1)]
ResultValue = str | dict[str, object] | list[object]
CheckpointText = Annotated[str, Field(max_length=4000)]
CheckpointValue = Annotated[
    CheckpointText | dict[str, object] | list[object],
    WithJsonSchema(
        {
            "oneOf": [
                {"type": "string", "maxLength": 4000},
                {"type": "object"},
                {"type": "array"},
            ]
        }
    ),
]
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
    resource_context: dict[str, object] | None = None
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
    resource_context: dict[str, object] | None = None
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
}


class TaskToolArguments(ArgModelBase):
    """FastMCP boundary model and public discovery schema for task."""

    request: TaskRequest
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    _validation_error: ValidationError | None = PrivateAttr(default=None)
    _raw_arguments: dict[str, object] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def capture_validation_error(cls, value, handler):
        raw_arguments = dict(value) if isinstance(value, dict) else {"$input": value}
        try:
            model = handler(value)
        except ValidationError as exc:
            model = cls.model_construct(request=None)
            model._validation_error = exc
        model._raw_arguments = raw_arguments
        return model

    def model_dump_one_level(self) -> dict[str, object]:
        # FuncMetadata.call_fn_with_arg_validation() forwards this dict to the tool function.
        return {"boundary": self}

    @property
    def validation_error(self) -> ValidationError | None:
        return self._validation_error

    @property
    def raw_arguments(self) -> dict[str, object]:
        return self._raw_arguments


def install_task_input_contract(mcp) -> None:
    """Make the same model authoritative for FastMCP execution and discovery."""

    tool = {item.name: item for item in mcp._tool_manager.list_tools()}["task"]
    tool.fn_metadata.arg_model = TaskToolArguments
    tool.parameters = TaskToolArguments.model_json_schema()


def task_request_action(request: object) -> str:
    if isinstance(request, TaskToolArguments):
        if request.validation_error is None:
            request = request.request
        else:
            request = request.raw_arguments.get("request")
    if isinstance(request, dict):
        action = request.get("action")
    else:
        action = getattr(request, "action", None)
    return action if isinstance(action, str) and action in TASK_ACTIONS else "unknown"


def _error_path(location: tuple[object, ...], request: object, message: str) -> str:
    parts = list(location)
    action = task_request_action(request)
    if len(parts) >= 2 and parts[0] == "request" and parts[1] == action:
        parts.pop(1)
    elif parts and parts[0] == action:
        parts.pop(0)

    fallback_field = None
    if "blocker_reason is required" in message:
        fallback_field = "blocker_reason"
    elif "result is required" in message:
        fallback_field = "result"
    elif "archive_note or note is required" in message:
        fallback_field = "archive_note"
    if fallback_field and (not parts or parts == ["request"]):
        if not parts:
            parts = ["request"]
        parts.append(fallback_field)

    if not parts:
        return "$"
    path = str(parts[0])
    for part in parts[1:]:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def task_validation_error(exc: ValidationError, request: object) -> dict[str, object]:
    errors = []
    for item in exc.errors(include_url=False, include_input=False):
        message = str(item.get("msg") or "invalid value")
        errors.append(
            {
                "error_class": str(item.get("type") or "validation_error"),
                "path": _error_path(tuple(item.get("loc") or ()), request, message),
                "description": message,
            }
        )
    return {
        "ok": False,
        "code": "validation_error",
        "error": "task request validation failed",
        "validation_errors": errors,
    }


def task_request_to_backend(request: TaskRequest) -> tuple[str, str, str | None, dict[str, object]]:
    data = request.model_dump(exclude_none=True)
    action = data.pop("action")
    code = data.pop("code")
    namespace = data.pop("namespace")
    task_id = data.pop("task_id", None)
    return code, namespace, task_id, {"action": action, **data}

"""FastMCP argument validation; canonical request models live in application."""

from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict, PrivateAttr, ValidationError, model_validator

from terminal_mcp.application.task_requests import (
    TASK_ACTIONS as TASK_ACTIONS,
)
from terminal_mcp.application.task_requests import (
    AccessCode as AccessCode,
)
from terminal_mcp.application.task_requests import (
    CheckpointText as CheckpointText,
)
from terminal_mcp.application.task_requests import (
    CheckpointValue as CheckpointValue,
)
from terminal_mcp.application.task_requests import (
    ExpectedRevision as ExpectedRevision,
)
from terminal_mcp.application.task_requests import (
    InputRefs as InputRefs,
)
from terminal_mcp.application.task_requests import (
    Namespace as Namespace,
)
from terminal_mcp.application.task_requests import (
    OutputRefs as OutputRefs,
)
from terminal_mcp.application.task_requests import (
    ResultValue as ResultValue,
)
from terminal_mcp.application.task_requests import (
    StrictTaskModel as StrictTaskModel,
)
from terminal_mcp.application.task_requests import (
    Tag as Tag,
)
from terminal_mcp.application.task_requests import (
    Tags as Tags,
)
from terminal_mcp.application.task_requests import (
    TaskArchiveRequest as TaskArchiveRequest,
)
from terminal_mcp.application.task_requests import (
    TaskCheckpointRequest as TaskCheckpointRequest,
)
from terminal_mcp.application.task_requests import (
    TaskClaimRequest as TaskClaimRequest,
)
from terminal_mcp.application.task_requests import (
    TaskCommentRequest as TaskCommentRequest,
)
from terminal_mcp.application.task_requests import (
    TaskCreateRequest as TaskCreateRequest,
)
from terminal_mcp.application.task_requests import (
    TaskDependencies as TaskDependencies,
)
from terminal_mcp.application.task_requests import (
    TaskDependency as TaskDependency,
)
from terminal_mcp.application.task_requests import (
    TaskDoneRequest as TaskDoneRequest,
)
from terminal_mcp.application.task_requests import (
    TaskId as TaskId,
)
from terminal_mcp.application.task_requests import (
    TaskIdentityRequest as TaskIdentityRequest,
)
from terminal_mcp.application.task_requests import (
    TaskRef as TaskRef,
)
from terminal_mcp.application.task_requests import (
    TaskRelateRequest as TaskRelateRequest,
)
from terminal_mcp.application.task_requests import (
    TaskRelationRequest as TaskRelationRequest,
)
from terminal_mcp.application.task_requests import (
    TaskReleaseRequest as TaskReleaseRequest,
)
from terminal_mcp.application.task_requests import (
    TaskRequest as TaskRequest,
)
from terminal_mcp.application.task_requests import (
    TaskReviewRequest as TaskReviewRequest,
)
from terminal_mcp.application.task_requests import (
    TaskRevisionRequest as TaskRevisionRequest,
)
from terminal_mcp.application.task_requests import (
    TaskStateRequest as TaskStateRequest,
)
from terminal_mcp.application.task_requests import (
    TaskUnrelateRequest as TaskUnrelateRequest,
)
from terminal_mcp.application.task_requests import (
    TaskUpdateRequest as TaskUpdateRequest,
)
from terminal_mcp.application.task_requests import (
    task_request_to_backend as task_request_to_backend,
)
from terminal_mcp.core.public_errors import (
    MAX_VALIDATION_ISSUES,
    PUBLIC_FIELDS,
    ValidationRepair,
    public_error,
    validation_issue,
)


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
    first = parts[0]
    path = str(first) if isinstance(first, str) and first in PUBLIC_FIELDS else "*"
    for part in parts[1:]:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            safe = part if isinstance(part, str) and part in PUBLIC_FIELDS else "*"
            path += f".{safe}"
    return path


def task_validation_error(exc: ValidationError, request: object) -> dict[str, object]:
    issues = []
    for item in exc.errors(include_url=False, include_input=False)[:MAX_VALIDATION_ISSUES]:
        raw_message = str(item.get("msg") or "")
        issues.append(
            validation_issue(
                item,
                path=_error_path(tuple(item.get("loc") or ()), request, raw_message),
            )
        )
    details = ValidationRepair(validation_errors=tuple(issues)) if issues else None
    return public_error("validation_error", details=details).as_dict()

"""Role endpoint input contracts and deterministic ChatGPT planning schemas."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from terminal_mcp.api_models import (
    ReviewDimension,
    ReviewVerdict,
    TaskLane,
    TaskPriority,
    TaskState,
)
from terminal_mcp.application.input_limits import (
    MAX_COMMAND_CHARS,
    MAX_HASH_CHARS,
    MAX_IDENTIFIER_CHARS,
    MAX_MESSAGE_TEXT_CHARS,
    MAX_OPAQUE_CURSOR_CHARS,
    MAX_QUEUE_ID,
    MAX_TAG_CHARS,
    MAX_TAG_ITEMS,
    MAX_TASK_SCOPE_CHARS,
)
from terminal_mcp.application.task_requests import (
    CheckpointValue,
    ExpectedRevision,
    InputRefs,
    OutputRefs,
    ResourceContext,
    ResultValue,
    ReviewEvidence,
    TaskDependencies,
)
from terminal_mcp.core.public_errors import (
    PUBLIC_FIELDS,
    ValidationRepair,
    public_error,
)
from terminal_mcp.core.public_errors import (
    validation_error as bounded_validation_error,
)
from terminal_mcp.core.read_contract import (
    DEFAULT_CMD_READ_LINES,
    DEFAULT_PAGE_LIMIT,
    MAX_CMD_READ_LINES,
    MAX_PAGE_LIMIT,
)

Namespace = Annotated[str, Field(min_length=1, max_length=120)]
TaskId = Annotated[str, Field(min_length=1, max_length=120)]
Identifier = Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_CHARS)]
Hash = Annotated[str, Field(min_length=1, max_length=MAX_HASH_CHARS)]
Cursor = Annotated[str, Field(max_length=MAX_OPAQUE_CURSOR_CHARS)]
Tags = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_TAG_CHARS)]],
    Field(max_length=MAX_TAG_ITEMS),
]


class StrictRoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(StrictRoleInput):
    action: Literal["start", "end", "interrupt"]


class TaskListInput(StrictRoleInput):
    namespace: Namespace | None = None
    lane: TaskLane | None = None
    state: TaskState | None = None
    tags: Tags | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: Cursor | None = None


class CommandRunInput(StrictRoleInput):
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_CHARS)]
    queue_id: Annotated[int | None, Field(ge=1, le=MAX_QUEUE_ID)] = None
    task_scope: Annotated[str, Field(min_length=1, max_length=MAX_TASK_SCOPE_CHARS)] = "none"


class CommandReadInput(StrictRoleInput):
    cmd_hash: Hash
    limit: Annotated[int, Field(ge=1, le=MAX_CMD_READ_LINES)] = DEFAULT_CMD_READ_LINES
    cursor: Cursor | None = None


class CommandCancelInput(StrictRoleInput):
    cmd_hash: Hash


class CommandRecoveryInput(StrictRoleInput):
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_CHARS)]


class TaskClaimInput(StrictRoleInput):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "claim"}}, "required": ["action"]},
                    "then": {"required": ["claim_intent"]},
                }
            ]
        },
    )

    action: Literal["claim", "release"]
    namespace: Namespace
    task_id: TaskId
    expected_revision: ExpectedRevision | None = None
    claim_intent: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    release_reason: Annotated[str | None, Field(min_length=1, max_length=4000)] = None


class TaskStateInput(StrictRoleInput):
    namespace: Namespace
    task_id: TaskId
    state: TaskState
    expected_revision: ExpectedRevision | None = None
    blocker_reason: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    result: ResultValue | None = None


class TaskCommentInput(StrictRoleInput):
    namespace: Namespace
    task_id: TaskId
    comment_text: Annotated[str, Field(min_length=1, max_length=4000)]


class MessageInput(StrictRoleInput):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "send"}}, "required": ["action"]},
                    "then": {"required": ["text"]},
                },
                {
                    "if": {"properties": {"action": {"const": "ack"}}, "required": ["action"]},
                    "then": {"required": ["message_hash"]},
                },
                {
                    "if": {"properties": {"action": {"const": "reply"}}, "required": ["action"]},
                    "then": {"required": ["message_hash", "text"]},
                },
            ]
        },
    )

    action: Literal["send", "read", "ack", "reply", "history", "recipients"]
    text: Annotated[str | None, Field(max_length=MAX_MESSAGE_TEXT_CHARS)] = None
    target: Identifier | None = None
    message_hash: Hash | None = None
    mode: Literal["notify", "ack", "alert"] | None = None
    require_reply: bool = False
    alert: bool = False
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: Cursor | None = None
    detail: Literal["summary", "full"] = "summary"
    namespace: Namespace | None = None
    task_id: TaskId | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.action == "send":
            if self.text is None:
                raise ValueError("text is required for action=send")
            if self.message_hash is not None:
                raise ValueError("message_hash is not allowed for action=send")
        elif self.action in {"read", "history", "recipients"}:
            if self.text is not None or self.message_hash is not None:
                raise ValueError(
                    "text and message_hash are not allowed for read/history/recipients"
                )
        elif self.action == "ack":
            if self.message_hash is None:
                raise ValueError("message_hash is required for action=ack")
            if self.text is not None:
                raise ValueError("text is not allowed for action=ack")
        elif self.action == "reply":
            if self.message_hash is None:
                raise ValueError("message_hash is required for action=reply")
            if self.text is None:
                raise ValueError("text is required for action=reply")
        return self


class TaskGetInput(StrictRoleInput):
    namespace: Namespace
    task_id: TaskId
    detail: Literal["snapshot", "detail", "history"] = "snapshot"
    history_kind: Literal["comments", "events", "reviews", "output_states"] = "events"
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: Cursor | None = None


class TaskManageInput(StrictRoleInput):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "create"}}, "required": ["action"]},
                    "then": {"required": ["isolation_hint"]},
                },
                {
                    "if": {
                        "properties": {
                            "action": {
                                "enum": [
                                    "update",
                                    "checkpoint",
                                    "done",
                                    "archive",
                                    "review",
                                    "relate",
                                    "unrelate",
                                    "state",
                                    "comment",
                                ]
                            }
                        },
                        "required": ["action"],
                    },
                    "then": {"required": ["task_id"]},
                },
                {
                    "if": {
                        "properties": {"action": {"const": "checkpoint"}},
                        "required": ["action"],
                    },
                    "then": {"required": ["checkpoint"]},
                },
                {
                    "if": {
                        "properties": {"action": {"enum": ["relate", "unrelate"]}},
                        "required": ["action"],
                    },
                    "then": {"required": ["relation_kind", "related_task_id"]},
                },
                {
                    "if": {"properties": {"action": {"const": "state"}}, "required": ["action"]},
                    "then": {"required": ["state"]},
                },
                {
                    "if": {"properties": {"action": {"const": "review"}}, "required": ["action"]},
                    "then": {"required": ["dimensions", "verdict"]},
                },
                {
                    "if": {"properties": {"action": {"const": "comment"}}, "required": ["action"]},
                    "then": {"required": ["comment_text"]},
                },
                {
                    "if": {"properties": {"action": {"const": "archive"}}, "required": ["action"]},
                    "then": {
                        "anyOf": [
                            {"required": ["archive_note"]},
                            {"required": ["note"]},
                        ]
                    },
                },
            ]
        },
    )

    action: Literal[
        "create",
        "update",
        "checkpoint",
        "done",
        "archive",
        "review",
        "relate",
        "unrelate",
        "state",
        "comment",
    ]
    namespace: Namespace
    task_id: TaskId | None = None
    expected_revision: ExpectedRevision | None = None
    isolation_hint: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    title: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    lane: TaskLane | None = None
    priority: TaskPriority | None = None
    state: TaskState | None = None
    description: Annotated[str | None, Field(max_length=8000)] = None
    next_action: Annotated[str | None, Field(max_length=2000)] = None
    resource_context: ResourceContext | None = None
    cooperative: bool | None = None
    checkpoint: CheckpointValue | None = None
    candidate_ref: Annotated[str | None, Field(max_length=200)] = None
    input_refs: InputRefs | None = None
    output_refs: OutputRefs | None = None
    result: ResultValue | None = None
    tags: Tags | None = None
    dependencies: TaskDependencies | None = None
    blocker_reason: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    relation_kind: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    related_namespace: Namespace | None = None
    related_task_id: TaskId | None = None
    archive_note: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    note: Annotated[str | None, Field(min_length=1, max_length=2000)] = None
    dimensions: Annotated[
        list[ReviewDimension] | None,
        Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}),
    ] = None
    verdict: ReviewVerdict | None = None
    evidence: ReviewEvidence | None = None
    comment_text: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    force: bool = False
    force_reason: Annotated[str | None, Field(min_length=1, max_length=2000)] = None


class TaskGraphInput(StrictRoleInput):
    namespace: Namespace
    task_id: TaskId
    direction: Literal["both", "incoming", "outgoing"] = "both"
    kinds: (
        Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=32)]
        | None
    ) = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT
    cursor: Cursor | None = None


class AgentObserveInput(StrictRoleInput):
    pass


class HealthInput(StrictRoleInput):
    extended: bool = False


class RuntimeBoundary(ArgModelBase):
    """Permissive FastMCP envelope; authoritative validation runs inside the handler."""

    model_config = ConfigDict(extra="allow")
    _raw_arguments: dict[str, object] = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def capture(cls, value, handler):
        model = handler(value)
        model._raw_arguments = dict(value) if isinstance(value, dict) else {"$input": value}
        return model

    def model_dump_one_level(self) -> dict[str, object]:
        return {"boundary": self}

    @property
    def raw_arguments(self) -> dict[str, object]:
        return self._raw_arguments


ROLE_TOOL_MODELS: dict[tuple[str, str], type[StrictRoleInput]] = {
    ("executor", "session"): SessionInput,
    ("executor", "task_list"): TaskListInput,
    ("executor", "command_run"): CommandRunInput,
    ("executor", "command_read"): CommandReadInput,
    ("executor", "command_cancel"): CommandCancelInput,
    ("executor", "command_recovery"): CommandRecoveryInput,
    ("executor", "task_claim"): TaskClaimInput,
    ("executor", "task_state"): TaskStateInput,
    ("executor", "task_comment"): TaskCommentInput,
    ("executor", "message"): MessageInput,
    ("coordinator", "session"): SessionInput,
    ("coordinator", "task_get"): TaskGetInput,
    ("coordinator", "task_list"): TaskListInput,
    ("coordinator", "task_manage"): TaskManageInput,
    ("coordinator", "task_graph"): TaskGraphInput,
    ("coordinator", "agent_observe"): AgentObserveInput,
    ("coordinator", "message"): MessageInput,
    ("coordinator", "health"): HealthInput,
}


ROLE_TOOL_DESCRIPTIONS: dict[tuple[str, str], str] = {
    (
        "executor",
        "session",
    ): "Start, end, or interrupt the managed WorkSession for the server-resolved LogicalAgent.",
    (
        "executor",
        "task_list",
    ): "Read bounded TaskListItem records for work orientation; the agent selects work.",
    (
        "executor",
        "command_run",
    ): "Run a command; fast completion may include the first bounded output page.",
    (
        "executor",
        "command_read",
    ): "Read bounded command status/output using opaque cursor pagination.",
    ("executor", "command_cancel"): "Cancel a command owned by the current LogicalAgent.",
    ("executor", "command_recovery"): "Run a recovery command under the current managed session.",
    (
        "executor",
        "task_claim",
    ): "Claim/release a task. claim uses claim_intent; release may use release_reason.",
    (
        "executor",
        "task_state",
    ): "Apply the canonical task state machine for a task owned by the current LogicalAgent.",
    ("executor", "task_comment"): "Append a comment/checkpoint note to canonical task history.",
    (
        "executor",
        "message",
    ): "Managed inbox: send needs text; ack needs message_hash; reply needs both.",
    (
        "coordinator",
        "session",
    ): "Start, end, or interrupt the managed WorkSession for the server-resolved LogicalAgent.",
    ("coordinator", "task_get"): "Read one canonical task as snapshot, detail, or history.",
    ("coordinator", "task_list"): "Read a bounded page of canonical TaskListItem records.",
    (
        "coordinator",
        "task_manage",
    ): "Apply a task mutation. create needs isolation_hint; existing tasks need task_id. "
    "checkpoint needs checkpoint; state needs state; comment needs comment_text; "
    "review needs dimensions/verdict; relate/unrelate need relation_kind/related_task_id; "
    "archive needs archive_note.",
    ("coordinator", "task_graph"): "Read a bounded dependency/relation graph rooted at one task.",
    (
        "coordinator",
        "agent_observe",
    ): "Read compact current LogicalAgent, WorkWindow, and WorkSession state.",
    (
        "coordinator",
        "message",
    ): "Managed inbox: send needs text; ack needs message_hash; reply needs both.",
    ("coordinator", "health"): "Read compact health; set extended=true for canonical diagnostics.",
}


_SESSION_BOUND_ROLE_TOOLS = frozenset(
    {
        ("executor", "task_list"),
        ("executor", "command_run"),
        ("executor", "command_cancel"),
        ("executor", "command_recovery"),
        ("executor", "task_claim"),
        ("executor", "task_state"),
        ("executor", "task_comment"),
        ("executor", "message"),
        ("coordinator", "task_get"),
        ("coordinator", "task_list"),
        ("coordinator", "task_manage"),
        ("coordinator", "task_graph"),
        ("coordinator", "agent_observe"),
        ("coordinator", "message"),
    }
)
for _key in _SESSION_BOUND_ROLE_TOOLS:
    ROLE_TOOL_DESCRIPTIONS[_key] += (
        " Requires an active managed session; call session(action='start') first."
    )


def _planning_hint(schema: dict, definitions: dict) -> str:
    if "$ref" in schema:
        return _planning_hint(definitions.get(schema["$ref"].rsplit("/", 1)[-1], {}), definitions)
    if "enum" in schema:
        return " | ".join(map(str, schema["enum"]))
    if "const" in schema:
        return str(schema["const"])
    if "anyOf" in schema:
        return " | ".join(
            dict.fromkeys(
                _planning_hint(item, definitions)
                for item in schema["anyOf"]
                if item.get("type") != "null"
            )
        )
    return schema.get("type", "JSON value")


def planning_schema(model: type[StrictRoleInput]) -> dict:
    """Publish argument names and hints; runtime owns all value validation."""
    if model is TaskManageInput:
        from terminal_mcp.application.base import ROLE_TASK_ACTIONS
        from terminal_mcp.mcp.task_planning import task_action_planning_schema

        return task_action_planning_schema(
            actions=ROLE_TASK_ACTIONS["coordinator"], exclude_fields={"code"}
        )
    runtime = model.model_json_schema()
    definitions = runtime.get("$defs", {})
    properties = {}
    for name, field in runtime.get("properties", {}).items():
        hint = field.get("description") or _planning_hint(field, definitions)
        entry = {"description": hint} if hint else {}
        if "default" in field:
            entry["default"] = field["default"]
        properties[name] = entry
    return {"type": "object", "properties": properties}


def serialized_schema(schema: dict) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def schema_digest(schema: dict) -> str:
    return hashlib.sha256(serialized_schema(schema)).hexdigest()


def schema_contract(role: str, tool_name: str) -> dict[str, object]:
    model = ROLE_TOOL_MODELS[(role, tool_name)]
    runtime_schema = model.model_json_schema()
    published_schema = planning_schema(model)
    from terminal_mcp.operation_metadata import tool_action_effects

    published_schema["x-terminal-mcp-action-matrix"] = tool_action_effects(role, tool_name)
    return {
        "tool_name": tool_name,
        "endpoint_role": role,
        "contract_version": 1,
        "runtime_input_schema_digest": schema_digest(runtime_schema),
        "planning_input_schema_digest": schema_digest(published_schema),
        "planning_input_schema_bytes": len(serialized_schema(published_schema)),
    }


def install_role_input_contract(mcp, role: str, *, overrides=None) -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    for (endpoint_role, tool_name), model in {**ROLE_TOOL_MODELS, **(overrides or {})}.items():
        if endpoint_role != role:
            continue
        tool = tools[tool_name]
        tool.fn_metadata.arg_model = RuntimeBoundary
        tool.parameters = planning_schema(model)


def validation_error(exc: ValidationError, raw: dict[str, object]) -> dict[str, object]:
    issue = exc.errors(include_url=False, include_input=False)[0]
    error_type = str(issue.get("type") or "")
    location = tuple(issue.get("loc") or ())
    message = str(issue.get("msg") or "")

    if error_type == "missing":
        reason = "missing_required"
    elif error_type == "extra_forbidden":
        reason = "unexpected_field"
    elif error_type.endswith("_type") or error_type in {"model_type", "list_type", "dict_type"}:
        reason = "invalid_type"
    elif error_type in {"literal_error", "enum"}:
        reason = "invalid_value"
    else:
        reason = "constraint_violation"

    if location:
        path = ".".join(
            str(part) if part in PUBLIC_FIELDS or type(part) is int else "*" for part in location
        )
    else:
        path = "$"
        for candidate in (
            "issuer_node_id",
            "access_code",
            "mode",
            "code",
            "task_id",
            "isolation_hint",
            "claim_intent",
            "release_reason",
            "text",
            "message_hash",
            "checkpoint",
            "result",
            "archive_note",
            "dimensions",
            "verdict",
            "related_task_id",
            "relation_kind",
            "comment_text",
        ):
            if candidate in message:
                path = candidate
                break
    repair = bounded_validation_error(exc).details
    if isinstance(repair, ValidationRepair) and not location and path != "$":
        issues = list(repair.validation_errors)
        issues[0] = issues[0].model_copy(
            update={"path": path, "description": f"Provide {path} for this action."}
        )
        repair = ValidationRepair(validation_errors=tuple(issues))
    return public_error(
        "input_validation_failed", reason=reason, path=path, details=repair
    ).as_dict()


def validate_boundary(boundary: RuntimeBoundary, model: type[StrictRoleInput]):
    try:
        return model.model_validate(boundary.raw_arguments), None
    except ValidationError as exc:
        return None, validation_error(exc, boundary.raw_arguments)

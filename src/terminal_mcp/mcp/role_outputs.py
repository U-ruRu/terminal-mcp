"""Typed public output contracts for versioned role MCP endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any, ClassVar, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, model_serializer

from terminal_mcp.adapters.mcp_identity import current_provider_evidence
from terminal_mcp.core.provider_identity import ProviderIdentityRegistry
from terminal_mcp.core.public_errors import (
    ErrorOutcome,
    ErrorRepair,
    RecoveryAction,
    error_from_exception,
    normalize_public_error,
)
from terminal_mcp.core.task_projections import (
    Cursor,
    TaskDetail,
    TaskEventHistory,
    TaskListItem,
    TaskOutputHistory,
    TaskReceipt,
    TaskReviewHistory,
    TaskSnapshot,
    TaskWorkingSet,
    WorkflowWarning,
)
from terminal_mcp.mcp.output_contracts import (
    AccessError,
    CmdOutput,
    MessageOutput,
    SessionEndResult,
    SessionInterruptResult,
    SessionLifecycleInfo,
    SessionStartResult,
    TaskOutput,
    projected_result,
)
from terminal_mcp.mcp.output_planning import flat_output_schema


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _schema(annotation: Any) -> dict[str, Any]:
    schema = TypeAdapter(annotation).json_schema()
    schema.setdefault("type", "object")
    return schema


class _RoleOutput:
    __success_type__: ClassVar[Any]

    @classmethod
    def success_schema(cls) -> dict[str, Any]:
        return _schema(cls.__success_type__)


class ProviderFingerprint(_Strict):
    provider: str
    binding_key: str
    subject_fingerprint: str
    session_fingerprint: str


def session_provider_fingerprint() -> dict | None:
    evidence = current_provider_evidence()
    if evidence is None:
        return None
    try:
        identity = ProviderIdentityRegistry().resolve(evidence.provider, evidence.metadata)
        return {
            "provider": identity.provider,
            "binding_key": identity.binding_key,
            "subject_fingerprint": hashlib.sha256(
                ("subject:" + identity.subject).encode()
            ).hexdigest(),
            "session_fingerprint": hashlib.sha256(
                ("session:" + identity.conversation).encode()
            ).hexdigest(),
        }
    except Exception:
        logging.getLogger(__name__).exception("session_provider_diagnostic_unavailable")
        return None


class RoleSessionStartResult(SessionStartResult):
    provider_identity: ProviderFingerprint | None = None


RoleSessionSuccess = Annotated[
    RoleSessionStartResult | SessionEndResult | SessionInterruptResult,
    Field(discriminator="action"),
]


class RoleSessionOutput(_RoleOutput, RootModel[RoleSessionSuccess | AccessError]):
    __success_type__ = RoleSessionSuccess


class TaskListSuccess(_Strict):
    ok: Literal[True]
    tasks: list[TaskListItem]
    next_cursor: Cursor | None = None


TaskListWire = TaskListSuccess | AccessError


class TaskListOutput(_RoleOutput, RootModel[TaskListWire]):
    __success_type__ = TaskListSuccess


class TaskClaimSuccess(_Strict):
    ok: Literal[True]
    action: Literal["claim"]
    task: TaskWorkingSet
    warnings: list[WorkflowWarning] = Field(default_factory=list)
    session_lifecycle: SessionLifecycleInfo | None = None


class TaskReleaseSuccess(_Strict):
    ok: Literal[True]
    action: Literal["release"]
    task: TaskReceipt
    warnings: list[WorkflowWarning] = Field(default_factory=list)
    session_lifecycle: SessionLifecycleInfo | None = None


TaskClaimSuccessWire = Annotated[
    TaskClaimSuccess | TaskReleaseSuccess, Field(discriminator="action")
]


class TaskClaimOutput(_RoleOutput, RootModel[TaskClaimSuccessWire | AccessError]):
    __success_type__ = TaskClaimSuccessWire


class TaskGetSnapshotSuccess(_Strict):
    ok: Literal[True]
    detail: Literal["snapshot"]
    task: TaskSnapshot


class TaskGetDetailSuccess(_Strict):
    ok: Literal[True]
    detail: Literal["detail"]
    task: TaskDetail


TaskHistoryPage = TaskEventHistory | TaskReviewHistory | TaskOutputHistory


class TaskGetHistorySuccess(_Strict):
    ok: Literal[True]
    detail: Literal["history"]
    task: TaskHistoryPage


TaskGetSuccess = Annotated[
    TaskGetSnapshotSuccess | TaskGetDetailSuccess | TaskGetHistorySuccess,
    Field(discriminator="detail"),
]


class TaskGetOutput(_RoleOutput, RootModel[TaskGetSuccess | AccessError]):
    __success_type__ = TaskGetSuccess


class TaskGraphNode(_Strict):
    namespace: str
    task_id: str
    state: str | None = None

    @model_serializer(mode="wrap")
    def preserve_node_shape(self, handler):
        data = handler(self)
        data["state"] = self.state
        return data


class TaskGraphEdge(_Strict):
    direction: Literal["incoming", "outgoing"]
    kind: str
    namespace: str
    task_id: str
    state: str | None = None
    satisfied: bool | None = None


class TaskGraphSuccess(_Strict):
    ok: Literal[True]
    nodes: list[TaskGraphNode]
    edges: list[TaskGraphEdge]
    next_cursor: Cursor | None = None


class TaskGraphOutput(_RoleOutput, RootModel[TaskGraphSuccess | AccessError]):
    __success_type__ = TaskGraphSuccess


class LogicalAgentView(_Strict):
    logical_agent_id: str
    public_name: str
    authority_node_id: str


class WorkWindowView(_Strict):
    work_window_id: str
    state: str
    opened_at: str
    hard_expires_at: str
    remaining_seconds: int = Field(ge=0)
    revision: int = Field(ge=1)


class WorkSessionView(_Strict):
    work_session_id: str
    session_epoch: int = Field(ge=1)
    state: str
    started_at: str
    hard_expires_at: str
    role: Literal["executor", "coordinator", "legacy"]
    contract_version: int = Field(ge=1)


class AgentObserveSuccess(_Strict):
    ok: Literal[True]
    logical_agent: LogicalAgentView
    work_window: WorkWindowView
    work_session: WorkSessionView


class AgentObserveOutput(_RoleOutput, RootModel[AgentObserveSuccess | AccessError]):
    __success_type__ = AgentObserveSuccess


class CompactHealthTerminal(_Strict):
    ok: bool | None = None
    scheduler: str | None = None
    parallelism: int | None = None
    queue_size: int | None = None
    degraded: bool | None = None


class CompactHealthWorkflow(_Strict):
    ok: bool | None = None
    active_claims: int | None = None
    unreleased_claims: int | None = None
    stale_claims: int | None = None


class CompactHealthComponent(_Strict):
    id: str
    status: Literal["healthy", "degraded", "failed"]
    reason: str | None = None


class HealthIdentityDiagnostic(_Strict):
    provider: str | None = None
    metadata_fields: list[str] = Field(default_factory=list)
    binding_key: str | None = None
    subject_fingerprint: str | None = None
    session_fingerprint: str | None = None
    status: Literal["resolved", "unbound", "missing", "unavailable", "invalid"]
    code: str | None = None
    logical_agent_id: str | None = None
    public_name: str | None = None
    authority_node_id: str | None = None


class CompactHealthSuccess(_Strict):
    ok: bool
    healthy: bool | None = None
    application: str | None = None
    agent_name: str | None = None
    version: str | None = None
    storage: str | None = None
    status: Literal["healthy", "degraded", "failed"] | None = None
    components: list[CompactHealthComponent] = Field(default_factory=list)
    terminal: CompactHealthTerminal = Field(default_factory=CompactHealthTerminal)
    workflow: CompactHealthWorkflow = Field(default_factory=CompactHealthWorkflow)


class ExtendedHealthSuccess(CompactHealthSuccess):
    auth_mode: str | None = None
    identity: HealthIdentityDiagnostic


RoleHealthSuccess = ExtendedHealthSuccess | CompactHealthSuccess


class RoleHealthOutput(_RoleOutput, RootModel[RoleHealthSuccess | AccessError]):
    __success_type__ = RoleHealthSuccess


ROLE_OUTPUT_MODELS = {
    ("executor", "session"): RoleSessionOutput,
    ("executor", "task_list"): TaskListOutput,
    ("executor", "command_run"): CmdOutput,
    ("executor", "command_read"): CmdOutput,
    ("executor", "command_cancel"): CmdOutput,
    ("executor", "command_recovery"): CmdOutput,
    ("executor", "task_claim"): TaskClaimOutput,
    ("executor", "task_state"): TaskOutput,
    ("executor", "task_comment"): TaskOutput,
    ("executor", "message"): MessageOutput,
    ("coordinator", "session"): RoleSessionOutput,
    ("coordinator", "task_get"): TaskGetOutput,
    ("coordinator", "task_list"): TaskListOutput,
    ("coordinator", "task_manage"): TaskOutput,
    ("coordinator", "task_graph"): TaskGraphOutput,
    ("coordinator", "agent_observe"): AgentObserveOutput,
    ("coordinator", "message"): MessageOutput,
    ("coordinator", "health"): RoleHealthOutput,
}


logger = logging.getLogger(__name__)


class RoleErrorBody(_Strict):
    code: str
    message: str
    details: ErrorRepair | None = None
    outcome: ErrorOutcome
    retry: RecoveryAction
    reason: str | None = None
    path: str | None = None
    return_to_chat: bool | None = None


class RoleFailure(_Strict):
    ok: Literal[False]
    provider_identity: ProviderFingerprint | None = None
    error: RoleErrorBody
    required_action: Literal["ack", "reply"] | None = None
    message_hash: str | None = None
    text: str | None = None
    return_to_chat: bool | None = None


def role_error_result(raw: dict) -> CallToolResult:
    """Return a handled application failure as transport-successful structured data."""
    canonical = normalize_public_error(raw).as_dict()
    body = {key: value for key, value in canonical.items() if key not in {"ok", "error"}}
    pending = (
        canonical.get("details", {}).get("pending_messages", [])
        if isinstance(canonical.get("details"), dict) else []
    )
    first = pending[0] if pending else {}
    action = (
        canonical["details"].get("required_action")
        if isinstance(canonical.get("details"), dict) else None
    )
    data = RoleFailure(
        ok=False,
        error=RoleErrorBody.model_validate(body),
        provider_identity=session_provider_fingerprint(),
        required_action=action if action in {"ack", "reply"} else None,
        message_hash=first.get("message_hash"),
        text=first.get("text"),
        return_to_chat=canonical.get("return_to_chat"),
    ).model_dump(mode="json", exclude_none=True)
    return CallToolResult(
        isError=False,
        structuredContent=data,
        content=[
            TextContent(
                type="text", text=json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            )
        ],
    )


def install_role_output_contract(mcp, role: str, *, overrides=None) -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    for (endpoint_role, tool_name), output_model in {
        **ROLE_OUTPUT_MODELS,
        **(overrides or {}),
    }.items():
        if endpoint_role != role:
            continue
        tool = tools[tool_name]
        raw_fn = tool.fn

        async def contracted(*, _raw_fn=raw_fn, _model=output_model, _name=tool_name, **kwargs):
            try:
                raw = await _raw_fn(**kwargs)
                if raw.get("ok") is False:
                    return role_error_result(raw)
                if _name == "session" and role != "access" and raw.get("action") == "start":
                    diagnostic = session_provider_fingerprint()
                    if diagnostic is not None:
                        raw = {**raw, "provider_identity": diagnostic}
                result = projected_result(raw, _model, tool=_name, variant=role)
                if result.structuredContent.get("ok") is False:
                    return role_error_result(result.structuredContent)
                return result
            except Exception as exc:
                logger.exception("role_tool_failure role=%s tool=%s", role, _name)
                return role_error_result(error_from_exception(exc).as_dict())

        tool.fn = contracted
        wire_model = RootModel[output_model.__success_type__ | RoleFailure]
        tool.fn_metadata.output_model = wire_model
        tool.fn_metadata.output_schema = flat_output_schema(wire_model.model_json_schema())
        tool.fn_metadata.wrap_output = False

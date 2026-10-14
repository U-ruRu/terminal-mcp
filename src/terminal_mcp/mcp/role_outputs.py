"""Typed public output contracts for versioned role MCP endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any, ClassVar, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    TypeAdapter,
    model_serializer,
)

from terminal_mcp.adapters.mcp_identity import current_provider_evidence
from terminal_mcp.core.persistent_admission import current_admission_context
from terminal_mcp.core.provider_identity import ProviderIdentityRegistry
from terminal_mcp.core.public_errors import (
    ErrorOutcome,
    RecoveryAction,
    error_from_exception,
    public_error,
)
from terminal_mcp.core.task_projections import (
    Cursor,
    TaskEventHistory,
    TaskListItem,
    TaskOutputHistory,
    TaskReceipt,
    TaskReviewHistory,
    WorkflowWarning,
)
from terminal_mcp.mcp.output_contracts import (
    AccessError,
    CmdOutput,
    CmdRunOutput,
    MessageOutput,
    SessionEndResult,
    SessionInterruptResult,
    SessionLifecycleInfo,
    SessionStartResult,
    TaskOutput,
    projected_result,
)
from terminal_mcp.mcp.output_planning import flat_output_schema
from terminal_mcp.mcp.public_responses import (
    OperationNotices,
    PublicTaskDetail,
    PublicTaskHistory,
    PublicTaskSnapshot,
    ResponseErrorRepair,
    error_response,
)


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
        admission = current_admission_context()
        identity = ProviderIdentityRegistry().resolve(
            evidence.provider,
            evidence.metadata,
            principal_id=admission.principal_id if admission is not None else None,
        )
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


class TaskClaimSuccess(OperationNotices):
    ok: Literal[True]


class TaskReleaseSuccess(_Strict):
    ok: Literal[True]
    action: Literal["release"]
    task: TaskReceipt
    warnings: list[WorkflowWarning] = Field(default_factory=list)
    session_lifecycle: SessionLifecycleInfo | None = None


TaskClaimSuccessWire = TaskClaimSuccess


class TaskClaimOutput(_RoleOutput, RootModel[TaskClaimSuccessWire | AccessError]):
    __success_type__ = TaskClaimSuccessWire


class TaskStateSuccess(OperationNotices):
    ok: Literal[True]


class TaskStateOutput(_RoleOutput, RootModel[TaskStateSuccess | AccessError]):
    __success_type__ = TaskStateSuccess


class TaskCommentSuccess(OperationNotices):
    ok: Literal[True]


class TaskCommentOutput(_RoleOutput, RootModel[TaskCommentSuccess | AccessError]):
    __success_type__ = TaskCommentSuccess


class TaskGetSnapshotSuccess(OperationNotices):
    ok: Literal[True]
    task: PublicTaskSnapshot


class TaskGetDetailSuccess(OperationNotices):
    ok: Literal[True]
    task: PublicTaskDetail


TaskHistoryPage = TaskEventHistory | TaskReviewHistory | TaskOutputHistory


class TaskGetHistorySuccess(OperationNotices):
    ok: Literal[True]
    task: PublicTaskHistory


TaskGetSuccess = TaskGetSnapshotSuccess | TaskGetDetailSuccess | TaskGetHistorySuccess


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


class CompactHealthSuccess(OperationNotices):
    ok: Literal[True]
    healthy: bool
    application: str | None
    agent_name: str | None
    version: str | None
    storage: str | None


class ExtendedHealthSuccess(CompactHealthSuccess):
    components: list[dict[str, JsonValue]]
    terminal: dict[str, JsonValue]
    workflow: dict[str, JsonValue]


RoleHealthSuccess = ExtendedHealthSuccess | CompactHealthSuccess


class RoleHealthOutput(_RoleOutput, RootModel[RoleHealthSuccess | AccessError]):
    __success_type__ = RoleHealthSuccess


ROLE_OUTPUT_MODELS = {
    ("executor", "session"): RoleSessionOutput,
    ("executor", "task_list"): TaskListOutput,
    ("executor", "command_run"): CmdRunOutput,
    ("executor", "command_read"): CmdOutput,
    ("executor", "command_cancel"): CmdOutput,
    ("executor", "command_recovery"): CmdOutput,
    ("executor", "task_claim"): TaskClaimOutput,
    ("executor", "task_state"): TaskStateOutput,
    ("executor", "task_comment"): TaskCommentOutput,
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
    details: ResponseErrorRepair | None = None
    outcome: ErrorOutcome
    retry: RecoveryAction
    reason: str | None = None
    path: str | None = None
    expected: str | None = Field(default=None, max_length=96)
    return_to_chat: bool | None = None


class RoleFailure(OperationNotices):
    ok: Literal[False]
    provider_identity: ProviderFingerprint | None = None
    error: RoleErrorBody
    outcome: ErrorOutcome | None = None
    cmd_hash: str | None = Field(default=None, min_length=1, max_length=128)


_INTERNAL_SESSION_ERRORS = {
    # Internal lifecycle and slot internals must never appear in public MCP.
    # Keep all machine-specific codes in native storage and log traces.
    "window_cooldown": "session_expired",
    "slot_not_armed": "session_expired",
    "access_mesh_binding_conflict": "invalid_session_number",
    "access_mesh_slot_not_found": "invalid_session_number",
    "access_mesh_issuer_required": "invalid_session_number",
}


def role_error_result(raw: dict) -> CallToolResult:
    """Return a handled application failure as transport-successful structured data."""
    canonical = error_response(raw)
    exposed = _INTERNAL_SESSION_ERRORS.get(canonical["code"])
    if exposed:
        canonical.update(public_error(exposed, outcome=canonical["outcome"]).as_dict())
    notice_keys = set(OperationNotices.model_fields)
    body = {
        key: value
        for key, value in canonical.items()
        if key not in {"ok", "error", "cmd_hash", "_meta"} | notice_keys
    }
    input_failure = canonical["code"] in {"input_validation_failed", "input_admission_failed"}
    if input_failure:
        issues = (canonical.get("details") or {}).get("validation_errors") or []
        if issues:
            first = issues[0]
            expectation = first.get("expected")
            if not expectation and first.get("error_class") == "missing":
                expectation = "required"
            if not expectation:
                expectation = {
                    "int_type": "integer",
                    "string_type": "string",
                    "bool_type": "boolean",
                    "list_type": "array",
                    "dict_type": "object",
                }.get(first.get("error_class"))
            if expectation:
                body["expected"] = expectation
    data = RoleFailure(
        ok=False,
        error=RoleErrorBody.model_validate(body),
        outcome="not_committed" if input_failure else None,
        provider_identity=session_provider_fingerprint(),
        **{key: canonical[key] for key in notice_keys | {"cmd_hash"} if key in canonical},
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


async def _session_envelope(application, role: str) -> dict:
    """Attach public timer to the MCP envelope, separate from business result."""
    timer = {"remaining_time": "00:00"}
    if application is None or getattr(application.service, "access_mesh", None) is None:
        return timer
    try:
        from terminal_mcp.adapters.actor import actor_for
        from terminal_mcp.adapters.mcp_identity import current_provider_evidence

        mesh = application.service.access_mesh
        evidence = current_provider_evidence()
        actor = actor_for(
            application,
            transport="mcp",
            endpoint_role=role,
            contract_version=1,
            provider=evidence.provider if evidence else None,
            provider_metadata=evidence.metadata if evidence else {},
        )
        key = mesh.connection_key(actor)
        slot = (
            mesh.store.issuer_bound_slot(key) if role == "access" else mesh.store.attached_slot(key)
        )
        if slot is not None:
            info = mesh.store.observed_identity(slot, now=mesh.clock())
            cycle = info.get("session_lifecycle") or {}
            remaining = max(0, int(cycle.get("remaining_seconds") or 0))
            timer["remaining_time"] = f"{remaining // 60:02d}:{remaining % 60:02d}"
            if cycle.get("state") in {"warning", "draining"}:
                timer["warning"] = "Сессия заканчивается. Завершай текущую работу."
            elif cycle.get("state") in {"expired", "cooldown"}:
                timer["alert"] = "Время сессии истекло, возвращайся в чат с промежуточным отчётом."
    except Exception:
        logger.debug("Session envelope unavailable", exc_info=True)
    return timer


def install_role_output_contract(mcp, role: str, *, overrides=None, application=None) -> None:
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
                notice_meta = raw.get("_meta") or {}
                raw = {key: value for key, value in raw.items() if key != "_meta"}
                if raw.get("ok") is False:
                    result = role_error_result(raw)
                    result.meta = {**notice_meta, **await _session_envelope(application, role)}
                    return result
                if _name == "session" and role != "access" and raw.get("action") == "start":
                    diagnostic = session_provider_fingerprint()
                    if diagnostic is not None:
                        raw = {**raw, "provider_identity": diagnostic}
                result = projected_result(raw, _model, tool=_name, variant=role)
                if result.structuredContent.get("ok") is False:
                    result = role_error_result(result.structuredContent)
                result.meta = {
                    **(result.meta or {}),
                    **notice_meta,
                    **await _session_envelope(application, role),
                }
                return result
            except Exception as exc:
                logger.exception("role_tool_failure role=%s tool=%s", role, _name)
                result = role_error_result(error_from_exception(exc).as_dict())
                result.meta = await _session_envelope(application, role)
                return result

        tool.fn = contracted
        wire_model = RootModel[output_model.__success_type__ | RoleFailure]
        tool.fn_metadata.output_model = wire_model
        tool.fn_metadata.output_schema = flat_output_schema(wire_model.model_json_schema())
        tool.fn_metadata.wrap_output = False

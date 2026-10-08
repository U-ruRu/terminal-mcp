"""Versioned Executor and Coordinator MCP adapters over the shared Application API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.adapters.mcp_identity import current_mcp_request_id, current_provider_evidence
from terminal_mcp.application import get_application
from terminal_mcp.application.requests import (
    CmdCancelRequest,
    CmdReadRequest,
    CmdRecoveryRequest,
    CmdRunRequest,
)
from terminal_mcp.application.task_requests import (
    TaskArchiveRequest,
    TaskCheckpointRequest,
    TaskClaimRequest,
    TaskCommentRequest,
    TaskCreateRequest,
    TaskDoneRequest,
    TaskRelateRequest,
    TaskReleaseRequest,
    TaskReviewRequest,
    TaskStateRequest,
    TaskUnrelateRequest,
    TaskUpdateRequest,
)
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.provider_identity import ProviderIdentityError, ProviderIdentityRegistry
from terminal_mcp.core.public_errors import public_error
from terminal_mcp.core.read_contract import (
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
)
from terminal_mcp.core.task_projections import (
    TaskHistory,
    TaskRecord,
    TaskSnapshot,
    project_task_detail,
    project_task_working_set,
)
from terminal_mcp.mcp.access_contracts import (
    AttachInput,
    MeshCommandReadInput,
    MeshCommandReadOutput,
    MeshMessageInput,
    MeshObserveOutput,
    MeshSessionOutput,
    MeshTaskCommentInput,
)
from terminal_mcp.mcp.output_contracts import (
    _task_record as _normalize_task_record,
)
from terminal_mcp.mcp.output_contracts import (
    cmd_result,
    message_result,
    observe_result,
    session_result,
    task_result,
)
from terminal_mcp.mcp.role_contracts import (
    ROLE_TOOL_DESCRIPTIONS,
    ROLE_TOOL_MODELS,
    AgentObserveInput,
    CommandCancelInput,
    CommandReadInput,
    CommandRecoveryInput,
    CommandRunInput,
    HealthInput,
    MessageInput,
    RuntimeBoundary,
    SessionInput,
    TaskClaimInput,
    TaskCommentInput,
    TaskGetInput,
    TaskGraphInput,
    TaskListInput,
    TaskManageInput,
    TaskStateInput,
    install_role_input_contract,
    schema_contract,
    validate_boundary,
    validation_error,
)
from terminal_mcp.mcp.role_outputs import install_role_output_contract

RoleName = Literal["executor", "coordinator"]
EXECUTOR_TOOLS = (
    "session",
    "task_list",
    "command_run",
    "command_read",
    "command_cancel",
    "command_recovery",
    "task_claim",
    "task_state",
    "task_comment",
    "message",
)
COORDINATOR_TOOLS = (
    "session",
    "task_get",
    "task_list",
    "task_manage",
    "task_graph",
    "agent_observe",
    "message",
    "health",
)
ROLE_TOOLS = {"executor": EXECUTOR_TOOLS, "coordinator": COORDINATOR_TOOLS}

_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_MUTATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

# Mixed-mode tools are destructive if *any* action has irreversible effects.
_DURABLE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
# Arbitrary shell commands can modify state and contact external systems.
_SHELL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)
_CANCEL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

_TASK_MANAGE_MODELS = {
    "create": TaskCreateRequest,
    "update": TaskUpdateRequest,
    "checkpoint": TaskCheckpointRequest,
    "done": TaskDoneRequest,
    "archive": TaskArchiveRequest,
    "review": TaskReviewRequest,
    "relate": TaskRelateRequest,
    "unrelate": TaskUnrelateRequest,
    "state": TaskStateRequest,
    "comment": TaskCommentRequest,
}


def _transport_security(public_base_url: str) -> TransportSecuritySettings:
    parsed = urlparse(public_base_url)
    hostname = parsed.hostname or "127.0.0.1"
    hosts = list(
        dict.fromkeys(
            [
                parsed.netloc,
                hostname,
                f"{hostname}:443",
                "127.0.0.1",
                "127.0.0.1:8080",
                "localhost",
                "localhost:8080",
            ]
        )
    )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[public_base_url],
    )


def _actor(service, role: RoleName):
    evidence = current_provider_evidence()
    return actor_for(
        service,
        transport="mcp",
        endpoint_role=role,
        contract_version=1,
        provider=evidence.provider if evidence is not None else None,
        provider_metadata=evidence.metadata if evidence is not None else None,
        request_id=current_mcp_request_id(),
    )


def _structured(result):
    structured = getattr(result, "structuredContent", None)
    return structured if isinstance(structured, dict) else {}


def _validate(boundary: RuntimeBoundary, role: RoleName, tool_name: str):
    return validate_boundary(boundary, ROLE_TOOL_MODELS[(role, tool_name)])


def _task_request(payload: dict[str, object], model):
    try:
        return model.model_validate(payload), None
    except ValidationError as exc:
        return None, validation_error(exc, payload)


async def _task_list(application, actor, request: TaskListInput) -> dict:
    raw = await application.observe(
        actor,
        subject="tasks",
        namespace=request.namespace,
        lane=request.lane,
        state=request.state,
        tags=request.tags,
        detail="summary",
        show_done=request.state == "done",
        show_archived=False,
        limit=request.limit,
        cursor=request.cursor,
    )
    if not raw.get("ok"):
        return raw
    projected = _structured(observe_result(raw, "tasks", detail="summary"))
    return {
        "ok": True,
        "tasks": projected.get("tasks", []),
        "next_cursor": projected.get("next_cursor"),
    }


async def _message(application, actor, request: MessageInput) -> dict:
    operation = {
        "send": ManagedOperation.MESSAGE_SEND,
        "read": ManagedOperation.MESSAGE_READ,
        "history": ManagedOperation.MESSAGE_READ,
        "recipients": ManagedOperation.MESSAGE_READ,
        "ack": ManagedOperation.MESSAGE_ACK,
        "reply": ManagedOperation.MESSAGE_REPLY,
    }[request.action]
    resolution = await application.session_gate.resolve(actor, None, operation)
    if resolution.failure is not None:
        return resolution.failure
    identity = resolution.identity or {}
    sender = str(identity.get("public_name") or "")
    if not sender:
        return public_error("identity_not_bound").as_dict()

    history = request.action == "history"
    mesh_messages = getattr(application.service, "access_mesh_messages", None)
    if mesh_messages is not None:

        def project(raw):
            return _structured(
                message_result(
                    raw,
                    sender=sender,
                    text=request.text,
                    target=request.target,
                    message_hash=request.message_hash,
                    mode=request.mode,
                    require_reply=request.require_reply,
                    alert=request.alert,
                    show_all=history,
                    recipients=request.action == "recipients",
                )
            )

        def preflight(raw):
            projected = project(raw)
            return projected if projected.get("ok") is not True else None

        with actor.bind():
            raw = await mesh_messages.message(
                actor,
                text=request.text,
                target=request.target,
                message_hash=request.message_hash,
                mode=request.mode,
                require_reply=request.require_reply,
                alert=request.alert,
                scope=request.scope,
                history=history,
                recipients=request.action == "recipients",
                limit=request.limit,
                cursor=request.cursor,
                detail=request.detail,
                namespace=request.namespace,
                task_id=request.task_id,
                response_preflight=preflight,
            )
        if raw.get("ok"):
            await application.session_gate.touch_provider(actor)
        return project(raw)
    raw = await application.message(
        resolution.actor,
        sender=sender,
        code=None,
        text=request.text,
        target=request.target,
        message_hash=request.message_hash,
        mode=request.mode,
        require_reply=request.require_reply,
        alert=request.alert,
        history=history,
        limit=request.limit,
        cursor=request.cursor,
        detail=request.detail,
        namespace=request.namespace,
        task_id=request.task_id,
        recipients=request.action == "recipients",
    )
    if request.action == "recipients":
        return raw
    return _structured(
        message_result(
            raw,
            sender=sender,
            text=request.text,
            target=request.target,
            message_hash=request.message_hash,
            mode=request.mode,
            require_reply=request.require_reply,
            alert=request.alert,
            show_all=history,
        )
    )


def _task_record_from_observe(raw: dict) -> tuple[TaskRecord | None, dict | None]:
    if not raw.get("ok"):
        return None, raw
    task = raw.get("task")
    if not isinstance(task, dict):
        return None, {"ok": False, "code": "resource_not_found", "error": "resource_not_found"}
    try:
        # Normalize the internal record without serializing an oversized MCP
        # response; _task_get budgets the selected projection afterwards.
        return TaskRecord.model_validate(_normalize_task_record(task)), None
    except ValidationError:
        return None, public_error("internal_error").as_dict()


async def _task_get(application, actor, request: TaskGetInput) -> dict:
    if request.detail == "snapshot":
        raw = await application.observe(
            actor,
            subject="tasks",
            namespace=request.namespace,
            task_id=request.task_id,
            detail="summary",
            show_done=True,
            show_archived=True,
            limit=1,
        )
        if not raw.get("ok"):
            return raw
        projected = _structured(
            observe_result(raw, "tasks", detail="summary", task_id=request.task_id)
        )
        task = projected.get("task")
        return {"ok": True, "detail": "snapshot", "task": task}

    raw = await application.observe(
        actor,
        subject="tasks",
        namespace=request.namespace,
        task_id=request.task_id,
        detail="full",
        show_done=True,
        show_archived=True,
        limit=1,
        _defer_full_task_budget=True,
    )
    record, failure = _task_record_from_observe(raw)
    if failure is not None:
        return failure
    assert record is not None

    if request.detail == "detail":
        try:
            detail = project_task_detail(record).model_dump(mode="json", exclude_none=True)
            bounded_page(
                [detail],
                limit=1,
                cursor=None,
                scope={
                    "kind": "task-detail",
                    "namespace": request.namespace,
                    "task_id": request.task_id,
                },
            )
        except (ValidationError, OutputItemTooLarge):
            return public_error("output_item_too_large").as_dict()
        return {"ok": True, "detail": "detail", "task": detail}

    scope = {
        "kind": "task.history",
        "namespace": request.namespace,
        "task_id": request.task_id,
        "stream": request.history_kind,
    }
    streams = {
        "comments": list(record.comments or []),
        "events": list(record.events or []),
        "reviews": list(record.reviews or []),
        "output_states": list(record.output_states or []),
    }
    items = [
        item.model_dump(mode="json", exclude_none=True) for item in streams[request.history_kind]
    ]
    try:
        page, next_cursor = bounded_page(
            items, limit=request.limit, cursor=request.cursor, scope=scope
        )
    except InvalidCursor:
        return public_error("invalid_cursor").as_dict()
    except OutputItemTooLarge:
        return public_error("output_item_too_large").as_dict()
    history = TaskHistory.model_validate(
        {
            "kind": request.history_kind,
            "namespace": request.namespace,
            "task_id": request.task_id,
            "items": page,
            "next_cursor": next_cursor,
        }
    ).root
    return {
        "ok": True,
        "detail": "history",
        "task": history.model_dump(mode="json", exclude_none=True),
    }


async def _task_graph(application, actor, request: TaskGraphInput) -> dict:
    raw = await application.observe(
        actor,
        subject="tasks",
        namespace=request.namespace,
        task_id=request.task_id,
        detail="full",
        show_done=True,
        show_archived=True,
        limit=1,
        _defer_full_task_budget=True,
    )
    record, failure = _task_record_from_observe(raw)
    if failure is not None:
        return failure
    assert record is not None

    edges: list[dict[str, object]] = []
    if request.direction in {"both", "outgoing"}:
        for dep in record.dependencies or []:
            edges.append(
                {
                    "direction": "outgoing",
                    "kind": "depends_on",
                    "namespace": str(dep.namespace.root),
                    "task_id": str(dep.task_id.root),
                    "state": str(dep.state),
                    "satisfied": dep.satisfied,
                }
            )
    for relation in record.relations or []:
        if request.direction != "both" and relation.direction != request.direction:
            continue
        edges.append(
            {
                "direction": relation.direction,
                "kind": relation.kind,
                "namespace": str(relation.namespace.root),
                "task_id": str(relation.task_id.root),
            }
        )
    if request.kinds:
        allowed = set(request.kinds)
        edges = [item for item in edges if item["kind"] in allowed]

    scope = {
        "kind": "task.graph",
        "namespace": request.namespace,
        "task_id": request.task_id,
        "direction": request.direction,
        "kinds": sorted(request.kinds or []),
    }
    try:
        page, next_cursor = bounded_page(
            edges, limit=request.limit, cursor=request.cursor, scope=scope
        )
    except InvalidCursor:
        return public_error("invalid_cursor").as_dict()
    except OutputItemTooLarge:
        return public_error("output_item_too_large").as_dict()
    nodes = [{"namespace": request.namespace, "task_id": request.task_id, "state": record.state}]
    seen = {(request.namespace, request.task_id)}
    for edge in page:
        key = (str(edge["namespace"]), str(edge["task_id"]))
        if key in seen:
            continue
        seen.add(key)
        node = {"namespace": key[0], "task_id": key[1], "state": edge.get("state")}
        nodes.append(node)
    return {"ok": True, "nodes": nodes, "edges": page, "next_cursor": next_cursor}


def _compact_health(raw: dict) -> dict:
    terminal = raw.get("terminal") if isinstance(raw.get("terminal"), dict) else {}
    workflow = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else {}
    return {
        "ok": True,
        "healthy": bool(raw.get("ok")),
        "application": raw.get("application"),
        "agent_name": raw.get("agent_name"),
        "version": raw.get("version"),
        "storage": raw.get("storage"),
        "status": raw.get("status"),
        "components": raw.get("components") or [],
        "terminal": {
            key: terminal.get(key)
            for key in ("ok", "scheduler", "parallelism", "queue_size", "degraded")
            if key in terminal
        },
        "workflow": {
            key: workflow.get(key)
            for key in ("ok", "active_claims", "unreleased_claims", "stale_claims")
            if key in workflow
        },
    }


async def _health_identity(application, actor) -> dict:
    """Report bounded identity evidence without publishing provider identifiers."""
    fields = sorted(
        key
        for key in actor.provider_metadata
        if key in {"openai/subject", "openai/session", "openai/organization"}
    )
    diagnostic = {"provider": actor.provider, "metadata_fields": fields, "status": "missing"}
    if actor.provider is None or not actor.provider_metadata:
        return diagnostic
    try:
        evidence = ProviderIdentityRegistry().resolve(actor.provider, actor.provider_metadata)
        diagnostic["binding_key"] = evidence.binding_key
        diagnostic["subject_fingerprint"] = hashlib.sha256(
            ("subject:" + evidence.subject).encode()
        ).hexdigest()
        diagnostic["session_fingerprint"] = hashlib.sha256(
            ("session:" + evidence.conversation).encode()
        ).hexdigest()
    except ProviderIdentityError as exc:
        return {**diagnostic, "status": "invalid", "code": exc.code}
    try:
        async with asyncio.timeout(2):
            resolution = await application.session_gate.provider_identity(
                actor, ManagedOperation.OBSERVE
            )
        if resolution.failure:
            code = resolution.failure.get("code", "authority_unavailable")
            return {
                **diagnostic,
                "status": "unbound"
                if code in {"identity_not_bound", "session_required", "session_attach_required"}
                else "unavailable",
                "code": code,
            }
        identity = resolution.identity or {}
        return {
            **diagnostic,
            "status": "resolved",
            **{
                key: identity[key]
                for key in ("logical_agent_id", "public_name", "authority_node_id")
                if key in identity
            },
        }
    except Exception:
        logging.getLogger(__name__).exception("health_identity_unavailable")
        return {**diagnostic, "status": "unavailable", "code": "authority_unavailable"}


def role_schema_contract(role: RoleName) -> dict[str, object]:
    return {
        "endpoint_role": role,
        "contract_version": 1,
        "tools": [schema_contract(role, name) for name in ROLE_TOOLS[role]],
    }


def build_role_mcp(
    service,
    role: RoleName,
    public_base_url: str = "http://127.0.0.1:8080",
    auth_mode: str = "none",
):
    if role not in ROLE_TOOLS:
        raise ValueError("unsupported MCP endpoint role")
    application = get_application(service, auth_mode=auth_mode)
    mesh = getattr(application.service, "access_mesh", None)
    input_overrides = (
        {
            (role, "session"): AttachInput,
            (role, "message"): MeshMessageInput,
            **(
                {
                    (role, "command_read"): MeshCommandReadInput,
                    (role, "task_comment"): MeshTaskCommentInput,
                }
                if role == "executor"
                else {}
            ),
        }
        if mesh
        else {}
    )
    output_overrides = (
        {
            (role, "session"): MeshSessionOutput,
            **(
                {(role, "command_read"): MeshCommandReadOutput}
                if role == "executor"
                else {(role, "agent_observe"): MeshObserveOutput}
            ),
        }
        if mesh
        else {}
    )

    def _validate(boundary, endpoint_role, tool_name):
        model = input_overrides.get(
            (endpoint_role, tool_name), ROLE_TOOL_MODELS[(endpoint_role, tool_name)]
        )
        return validate_boundary(boundary, model)

    mcp = FastMCP(
        f"terminal-mcp-{role}-v1",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=_transport_security(public_base_url),
    )

    @mcp.tool(
        name="session",
        structured_output=False,
        annotations=_DURABLE,
        description=ROLE_TOOL_DESCRIPTIONS[(role, "session")],
    )
    async def role_session(boundary: RuntimeBoundary) -> dict:
        request, failure = _validate(boundary, role, "session")
        if failure is not None:
            return failure
        if mesh:
            actor = _actor(application, role)
            with actor.bind():
                return await mesh.attach(
                    actor, issuer_node_id=request.issuer_node_id, access_code=request.access_code
                )
        request = SessionInput.model_validate(request)
        raw = await application.session(
            _actor(application, role),
            action=request.action,
            mode="persistent" if request.action == "start" else None,
            code=None,
        )
        return _structured(session_result(raw, request.action))

    @mcp.tool(
        name="task_list",
        structured_output=False,
        annotations=_READ,
        description=ROLE_TOOL_DESCRIPTIONS[(role, "task_list")],
    )
    async def role_task_list(boundary: RuntimeBoundary) -> dict:
        request, failure = _validate(boundary, role, "task_list")
        if failure is not None:
            return failure
        return await _task_list(application, _actor(application, role), request)

    @mcp.tool(
        name="message",
        structured_output=False,
        annotations=_DURABLE,
        description=ROLE_TOOL_DESCRIPTIONS[(role, "message")],
    )
    async def role_message(boundary: RuntimeBoundary) -> dict:
        request, failure = _validate(boundary, role, "message")
        if failure is not None:
            return failure
        return await _message(application, _actor(application, role), request)

    if role == "executor":

        @mcp.tool(
            name="command_run",
            structured_output=False,
            annotations=_SHELL,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "command_run")],
        )
        async def command_run(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "command_run")
            if failure is not None:
                return failure
            request = CommandRunInput.model_validate(request)
            canonical = CmdRunRequest(
                action="run",
                code=None,
                command=request.command,
                queue_id=request.queue_id,
                task_scope=request.task_scope,
            )
            raw = await application.cmd(_actor(application, role), canonical)
            return _structured(cmd_result(raw, "run"))

        @mcp.tool(
            name="command_read",
            structured_output=False,
            annotations=_READ,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "command_read")],
        )
        async def command_read(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "command_read")
            if failure is not None:
                return failure
            if mesh and request.cmd_hash is None:
                return await application.commands.journal(
                    _actor(application, role), limit=request.limit, cursor=request.cursor
                )
            if not mesh:
                request = CommandReadInput.model_validate(request)
            canonical = CmdReadRequest(
                action="read",
                code=None,
                cmd_hash=request.cmd_hash,
                limit=request.limit,
                cursor=request.cursor,
            )
            raw = await application.cmd(_actor(application, role), canonical)
            return _structured(cmd_result(raw, "read"))

        @mcp.tool(
            name="command_cancel",
            structured_output=False,
            annotations=_CANCEL,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "command_cancel")],
        )
        async def command_cancel(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "command_cancel")
            if failure is not None:
                return failure
            request = CommandCancelInput.model_validate(request)
            canonical = CmdCancelRequest(action="cancel", code=None, cmd_hash=request.cmd_hash)
            raw = await application.cmd(_actor(application, role), canonical)
            return _structured(cmd_result(raw, "cancel"))

        @mcp.tool(
            name="command_recovery",
            structured_output=False,
            annotations=_SHELL,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "command_recovery")],
        )
        async def command_recovery(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "command_recovery")
            if failure is not None:
                return failure
            request = CommandRecoveryInput.model_validate(request)
            canonical = CmdRecoveryRequest(action="recovery", code=None, command=request.command)
            raw = await application.cmd(_actor(application, role), canonical)
            return _structured(cmd_result(raw, "recovery"))

        @mcp.tool(
            name="task_claim",
            structured_output=False,
            annotations=_MUTATE,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_claim")],
        )
        async def task_claim(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_claim")
            if failure is not None:
                return failure
            request = TaskClaimInput.model_validate(request)
            payload = request.model_dump(exclude_none=True)
            payload["code"] = None
            model = TaskClaimRequest if request.action == "claim" else TaskReleaseRequest
            canonical, failure = _task_request(payload, model)
            if failure is not None:
                return failure
            raw = await application.task(_actor(application, role), canonical)
            structured = _structured(task_result(raw, request.action))
            if (
                request.action == "claim"
                and structured.get("ok")
                and isinstance(structured.get("task"), dict)
            ):
                snapshot = TaskSnapshot.model_validate(structured["task"])
                structured["task"] = project_task_working_set(snapshot).model_dump(
                    mode="json", exclude_none=True
                )
            return structured

        @mcp.tool(
            name="task_state",
            structured_output=False,
            annotations=_DURABLE,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_state")],
        )
        async def task_state(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_state")
            if failure is not None:
                return failure
            request = TaskStateInput.model_validate(request)
            payload = request.model_dump(exclude_none=True)
            payload.update({"action": "state", "code": None})
            canonical, failure = _task_request(payload, TaskStateRequest)
            if failure is not None:
                return failure
            raw = await application.task(_actor(application, role), canonical)
            return _structured(task_result(raw, "state"))

        @mcp.tool(
            name="task_comment",
            structured_output=False,
            annotations=_DURABLE,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_comment")],
        )
        async def task_comment(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_comment")
            if failure is not None:
                return failure
            if mesh:
                canonical = request.to_request()
                raw = await application.task(_actor(application, role), canonical)
                return _structured(task_result(raw, canonical.action))
            request = TaskCommentInput.model_validate(request)
            canonical = TaskCommentRequest(
                action="comment",
                code=None,
                namespace=request.namespace,
                task_id=request.task_id,
                comment_text=request.comment_text,
            )
            raw = await application.task(_actor(application, role), canonical)
            return _structured(task_result(raw, "comment"))

    else:

        @mcp.tool(
            name="task_get",
            structured_output=False,
            annotations=_READ,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_get")],
        )
        async def task_get(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_get")
            if failure is not None:
                return failure
            return await _task_get(application, _actor(application, role), request)

        @mcp.tool(
            name="task_manage",
            structured_output=False,
            annotations=_DURABLE,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_manage")],
        )
        async def task_manage(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_manage")
            if failure is not None:
                return failure
            request = TaskManageInput.model_validate(request)
            payload = request.model_dump(exclude_none=True, exclude_unset=True)
            payload["code"] = None
            canonical, failure = _task_request(payload, _TASK_MANAGE_MODELS[request.action])
            if failure is not None:
                return failure
            raw = await application.task(_actor(application, role), canonical)
            return _structured(task_result(raw, request.action))

        @mcp.tool(
            name="task_graph",
            structured_output=False,
            annotations=_READ,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_graph")],
        )
        async def task_graph(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_graph")
            if failure is not None:
                return failure
            return await _task_graph(application, _actor(application, role), request)

        @mcp.tool(
            name="agent_observe",
            structured_output=False,
            annotations=_READ,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "agent_observe")],
        )
        async def agent_observe(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "agent_observe")
            if failure is not None:
                return failure
            AgentObserveInput.model_validate(request)
            if mesh:
                return await mesh.observe(_actor(application, role))
            return await application.agent_observe(_actor(application, role))

        @mcp.tool(
            name="health",
            structured_output=False,
            annotations=_READ,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "health")],
        )
        async def health(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "health")
            if failure is not None:
                return failure
            request = HealthInput.model_validate(request)
            actor = _actor(application, role)
            raw = await application.health(actor)
            if not raw.get("ok") and "status" not in raw:
                return raw
            identity = await _health_identity(application, actor)
            result = _compact_health(raw)
            result["agent_name"] = identity.get("public_name")
            if request.extended:
                result.update(auth_mode=raw.get("auth_mode"), identity=identity)
            return result

    install_role_input_contract(mcp, role, overrides=input_overrides)
    install_role_output_contract(mcp, role, overrides=output_overrides)
    if mesh:
        for tool in mcp._tool_manager.list_tools():
            text = tool.description or ""
            text = text.split(" Requires an active managed session;")[0]
            if tool.name == "session":
                tool.description = (
                    "Attach this connector once using issuer_node_id and access_code. "
                    "Access Code is mandatory; action is attach-only. "
                    "The local binding survives restart and automatic work-cycle rearm. "
                    "Manage issuance and end the shared access cycle through Access MCP. "
                    "Contract updated for Terminal MCP 0.14.1."
                )
                tool.annotations = ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                )
            elif tool.name == "task_comment":
                tool.description = (
                    "Append comment_text with action='comment' (the default), or save "
                    "checkpoint with action='checkpoint' and optional expected_revision. "
                    "Checkpoint updates preserve task state and history. "
                    "Writes require this connector's attached local work cycle."
                )
            elif tool.name == "command_read":
                tool.description = (
                    "Read any local command by cmd_hash, or omit cmd_hash for the local "
                    "command journal. Access Code and active work cycle are unnecessary. "
                    "Use the returned opaque cursor for the next bounded page."
                )
            elif tool.name in {"task_list", "task_get", "task_graph", "agent_observe", "health"}:
                tool.description = text + " Available without an active work cycle or Access Code."
            elif tool.name == "message":
                tool.description = (
                    "Read inbox, history, and recipients; send, acknowledge, or reply. "
                    "scope='fleet' targets local active participants first and configured "
                    "Mesh peers; scope='local' stays on this node. Writes use an attached "
                    "local work cycle. Alerts remain durable until reply."
                )
            else:
                tool.description = (
                    text + " Attach this connector once; writes use its local work cycle."
                )

    def _install_activity_touch() -> None:
        for tool in mcp._tool_manager.list_tools():
            raw_fn = tool.fn

            async def touched(*, _raw_fn=raw_fn, **kwargs):
                result = await _raw_fn(**kwargs)
                structured = getattr(result, "structuredContent", None)
                succeeded = (
                    structured.get("ok") is True
                    if isinstance(structured, dict)
                    else result.get("ok") is True
                    if isinstance(result, dict)
                    else False
                )
                if succeeded:
                    try:
                        async with asyncio.timeout(0.25):
                            await application.session_gate.touch_provider(_actor(application, role))
                    except TimeoutError:
                        logging.getLogger(__name__).debug(
                            "provider_activity_touch_timeout role=%s", role
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "provider_activity_touch_failed role=%s", role
                        )
                return result

            tool.fn = touched

    _install_activity_touch()

    registered = mcp._tool_manager._tools
    mcp._tool_manager._tools = {name: registered[name] for name in ROLE_TOOLS[role]}
    from terminal_mcp.operation_metadata import install_action_metadata

    install_action_metadata(mcp, role, mesh=bool(mesh))
    mcp.role_schema_contract = role_schema_contract(role)
    if mesh:
        from terminal_mcp.mcp.role_contracts import (
            schema_digest,
            serialized_schema,
        )

        mcp.role_schema_contract = {
            "endpoint_role": role,
            "contract_version": 1,
            "access_mesh": True,
            "tools": [
                {
                    "tool_name": name,
                    "runtime_input_schema_digest": schema_digest(
                        input_overrides.get(
                            (role, name), ROLE_TOOL_MODELS[(role, name)]
                        ).model_json_schema()
                    ),
                    "planning_input_schema_digest": schema_digest(registered[name].parameters),
                    "planning_input_schema_bytes": len(
                        serialized_schema(registered[name].parameters)
                    ),
                }
                for name in ROLE_TOOLS[role]
            ],
        }
    return mcp

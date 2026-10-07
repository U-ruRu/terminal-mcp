"""Versioned Executor and Coordinator MCP adapters over the shared Application API."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.adapters.mcp_identity import current_provider_evidence
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
from terminal_mcp.core.public_errors import public_error
from terminal_mcp.core.read_contract import InvalidCursor, decode_cursor, encode_cursor
from terminal_mcp.core.task_projections import (
    TaskHistory,
    TaskRecord,
    TaskSnapshot,
    project_task_detail,
    project_task_working_set,
)
from terminal_mcp.mcp.output_contracts import (
    cmd_result,
    health_result,
    message_result,
    observe_result,
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
    )
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
    projected = _structured(observe_result(raw, "tasks", detail="full", task_id="selected"))
    task = projected.get("task")
    if not isinstance(task, dict):
        return None, {"ok": False, "code": "resource_not_found", "error": "resource_not_found"}
    try:
        return TaskRecord.model_validate(task), None
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
    )
    record, failure = _task_record_from_observe(raw)
    if failure is not None:
        return failure
    assert record is not None

    if request.detail == "detail":
        detail = project_task_detail(record).model_dump(mode="json", exclude_none=True)
        return {"ok": True, "detail": "detail", "task": detail}

    scope = {
        "kind": "task.history",
        "namespace": request.namespace,
        "task_id": request.task_id,
        "stream": request.history_kind,
    }
    try:
        offset = decode_cursor(request.cursor, scope)
    except InvalidCursor:
        return {"ok": False, "code": "invalid_cursor", "error": "invalid_cursor"}

    streams = {
        "comments": list(record.comments or []),
        "events": list(record.events or []),
        "reviews": list(record.reviews or []),
        "output_states": list(record.output_states or []),
    }
    items = streams[request.history_kind]
    page = items[offset : offset + request.limit]
    next_cursor = (
        encode_cursor(offset + len(page), scope) if offset + len(page) < len(items) else None
    )
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
        offset = decode_cursor(request.cursor, scope)
    except InvalidCursor:
        return {"ok": False, "code": "invalid_cursor", "error": "invalid_cursor"}
    page = edges[offset : offset + request.limit]
    next_cursor = (
        encode_cursor(offset + len(page), scope) if offset + len(page) < len(edges) else None
    )
    nodes = [{"namespace": request.namespace, "task_id": request.task_id, "state": record.state}]
    seen = {(request.namespace, request.task_id)}
    for edge in page:
        key = (str(edge["namespace"]), str(edge["task_id"]))
        if key in seen:
            continue
        seen.add(key)
        node = {"namespace": key[0], "task_id": key[1]}
        if "state" in edge:
            node["state"] = edge["state"]
        nodes.append(node)
    return {"ok": True, "nodes": nodes, "edges": page, "next_cursor": next_cursor}


def _compact_health(raw: dict) -> dict:
    terminal = raw.get("terminal") if isinstance(raw.get("terminal"), dict) else {}
    workflow = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else {}
    return {
        "ok": bool(raw.get("ok")),
        "application": raw.get("application"),
        "version": raw.get("version"),
        "storage": raw.get("storage"),
        "terminal": {
            key: terminal.get(key)
            for key in ("ok", "scheduler", "parallelism", "queue_size", "degraded")
            if key in terminal
        },
        "workflow": {
            key: workflow.get(key)
            for key in ("ok", "by_state", "active_claims", "live_claims", "stale_claims")
            if key in workflow
        },
    }


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
        annotations=_MUTATE,
        description=ROLE_TOOL_DESCRIPTIONS[(role, "session")],
    )
    async def role_session(boundary: RuntimeBoundary) -> dict:
        request, failure = _validate(boundary, role, "session")
        if failure is not None:
            return failure
        request = SessionInput.model_validate(request)
        raw = await application.session(
            _actor(application, role),
            action=request.action,
            mode="persistent" if request.action == "start" else None,
            code=None,
        )
        return raw

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
        annotations=_MUTATE,
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
            annotations=_MUTATE,
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
            annotations=_MUTATE,
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
            annotations=_MUTATE,
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
            annotations=_MUTATE,
            description=ROLE_TOOL_DESCRIPTIONS[(role, "task_comment")],
        )
        async def task_comment(boundary: RuntimeBoundary) -> dict:
            request, failure = _validate(boundary, role, "task_comment")
            if failure is not None:
                return failure
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
            annotations=_MUTATE,
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
            raw = await application.health(_actor(application, role))
            if not raw.get("ok"):
                return raw
            return _structured(health_result(raw)) if request.extended else _compact_health(raw)

    install_role_input_contract(mcp, role)
    registered = mcp._tool_manager._tools
    mcp._tool_manager._tools = {name: registered[name] for name in ROLE_TOOLS[role]}
    mcp.role_schema_contract = role_schema_contract(role)
    return mcp

"""Thin MCP adapter over the transport-independent Application API."""

from typing import Annotated, Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.api_models import TaskLane, TaskOperationalStatus, TaskState
from terminal_mcp.application import get_application
from terminal_mcp.application.projections import (
    _finish_cmd_read_page as _finish_cmd_read_page,
)
from terminal_mcp.application.projections import (
    _read_error as _read_error,
)

# Retain import compatibility for existing integrations; implementation is canonical.
from terminal_mcp.application.projections import (
    _task_summary as _task_summary,
)
from terminal_mcp.application.requests import (
    CmdCancelRequest as CmdCancelRequest,
)
from terminal_mcp.application.requests import (
    CmdReadRequest as CmdReadRequest,
)
from terminal_mcp.application.requests import (
    CmdRecoveryRequest as CmdRecoveryRequest,
)
from terminal_mcp.application.requests import (
    CmdRequest as CmdRequest,
)
from terminal_mcp.application.requests import (
    CmdRunRequest as CmdRunRequest,
)
from terminal_mcp.application.requests import (
    ContextCreateRequest as ContextCreateRequest,
)
from terminal_mcp.application.requests import (
    ContextDeleteRequest as ContextDeleteRequest,
)
from terminal_mcp.application.requests import (
    ContextListRequest as ContextListRequest,
)
from terminal_mcp.application.requests import (
    ContextRequest as ContextRequest,
)
from terminal_mcp.application.requests import (
    ContextUpdateRequest as ContextUpdateRequest,
)
from terminal_mcp.core.read_contract import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from terminal_mcp.mcp.output_contracts import (
    CmdOutput,
    ContextOutput,
    HealthOutput,
    MessageOutput,
    ObserveOutput,
    SessionOutput,
    TaskOutput,
    cmd_result,
    context_result,
    health_result,
    install_public_output_contract,
    message_result,
    observe_result,
    session_result,
    task_result,
)
from terminal_mcp.mcp.task_contract import (
    TaskToolArguments,
    install_task_input_contract,
    task_request_action,
    task_validation_error,
)

_SAFE_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_SAFE_OPERATION = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


def _preflight_message_inbox_result(
    result: dict,
    *,
    sender: str,
    target: str | None,
    mode: str | None,
    require_reply: bool,
    alert: bool,
) -> dict | None:
    """Validate the final post-surface envelope before durable inbox mutation."""
    encoded = message_result(
        result,
        sender=sender,
        text=None,
        target=target,
        message_hash=None,
        mode=mode,
        require_reply=require_reply,
        alert=alert,
        show_all=False,
    )
    structured = encoded.structuredContent or {}
    if structured.get("ok") is False:
        return _read_error(
            str(structured.get("code") or "output_item_too_large"),
            "final message page cannot be returned within the public response budget",
        )
    return None


def build_mcp(
    service,
    public_base_url: str = "http://127.0.0.1:8080",
    auth_mode: str = "none",
    *,
    persistent_enabled: bool = False,
):
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
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[public_base_url],
    )
    mcp = FastMCP(
        "terminal-mcp",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=security,
    )

    application = get_application(service, auth_mode=auth_mode)

    @mcp.tool(
        name="session",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Start, end, or interrupt a unified Access session. start requires an explicit "
            "mode: persistent requires an existing Access code; legacy creates a temporary "
            "slot and returns its Access code once. end/interrupt require the Access code."
        ),
    )
    async def access_session_tool(
        action: Literal["start", "end", "interrupt"],
        mode: Literal["persistent", "legacy"] | None = None,
        code: Annotated[
            str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
        ] = None,
        display_name: Annotated[str | None, Field(max_length=80)] = None,
    ) -> dict:
        return await application.session(
            actor_for(service, transport="mcp"),
            action=action,
            mode=mode,
            code=code,
            display_name=display_name,
        )

    @mcp.tool(
        name="observe",
        structured_output=False,
        annotations=_SAFE_READ_ONLY,
        description=(
            "Read bounded unified session, task, or namespace state. Collection reads default "
            "to compact summary records and use opaque cursor pagination; request detail=full "
            "only for expanded domain state."
        ),
    )
    async def access_observe_tool(
        subject: Literal["sessions", "tasks", "namespaces"] = "sessions",
        namespace: str | None = None,
        task_id: str | None = None,
        lane: TaskLane | None = None,
        state: TaskState | None = None,
        operational_status: TaskOperationalStatus | None = None,
        tags: list[str] | None = None,
        detail: Literal["summary", "full"] = "summary",
        show_done: bool = False,
        show_archived: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        return await application.observe(
            actor_for(service, transport="mcp"),
            subject=subject,
            namespace=namespace,
            task_id=task_id,
            lane=lane,
            state=state,
            operational_status=operational_status,
            tags=tags,
            detail=detail,
            show_done=show_done,
            show_archived=show_archived,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(
        name="message",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Send, acknowledge, reply to, or read bounded agent messaging for an acti"
            "ve unified session using the same Access code used by cmd/task/context. "
            "A read (no text and "
            "no message_hash) defaults to the active inbox; history=true selects history. "
            "Read pages use limit/cursor and compact summary records by default."
        ),
    )
    async def access_message_tool(
        sender: str,
        code: Annotated[
            str | None, Field(min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
        ] = None,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        mode: Literal["notify", "ack", "alert"] | None = None,
        require_reply: bool = False,
        alert: bool = False,
        history: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        detail: Literal["summary", "full"] = "summary",
        namespace: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        return await application.message(
            actor_for(service, transport="mcp"),
            sender=sender,
            code=code,
            text=text,
            target=target,
            message_hash=message_hash,
            mode=mode,
            require_reply=require_reply,
            alert=alert,
            history=history,
            limit=limit,
            cursor=cursor,
            detail=detail,
            namespace=namespace,
            task_id=task_id,
            response_preflight=lambda preview: _preflight_message_inbox_result(
                preview,
                sender=sender,
                target=target,
                mode=mode,
                require_reply=require_reply,
                alert=alert,
            ),
        )

    @mcp.tool(
        name="task",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Mutate a managed task under an active unified Access session using a strict "
            "action-discriminated request. Claim ownership is durable per logical slot; "
            "WIP is one live managed-task claim per slot."
        ),
    )
    async def access_task_tool(boundary: TaskToolArguments) -> dict:
        if boundary.validation_error is not None:
            return task_validation_error(boundary.validation_error, boundary)
        return await application.task(actor_for(service, transport="mcp"), request=boundary.request)

    @mcp.tool(
        name="cmd",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Run, read, cancel, or execute recovery commands. run/cancel/recovery require "
            "an Access code; read may omit code for anonymous output access, or provide code "
            "to surface the caller's inbox. ack-required messages block run and alerts block "
            "work until reply."
        ),
    )
    async def access_cmd_tool(request: CmdRequest) -> dict:
        return await application.cmd(actor_for(service, transport="mcp"), request=request)

    @mcp.tool(
        name="context",
        structured_output=False,
        annotations=_SAFE_OPERATION,
        description=(
            "Read or mutate instance context using an action-discriminated request. list has "
            "no code field; create/update/delete require an active unified Access code."
        ),
    )
    async def access_context_tool(request: ContextRequest) -> dict:
        return await application.context(actor_for(service, transport="mcp"), request=request)

    @mcp.tool(
        name="health",
        structured_output=False,
        annotations=_SAFE_READ_ONLY,
        description="Return terminal service health without requiring an Access code.",
    )
    async def access_health_tool() -> dict:
        return await application.health(actor_for(service, transport="mcp"))

    install_task_input_contract(mcp)

    install_public_output_contract(
        mcp, "session", SessionOutput, lambda raw, kw: session_result(raw, kw["action"])
    )
    install_public_output_contract(
        mcp,
        "observe",
        ObserveOutput,
        lambda raw, kw: observe_result(
            raw, kw["subject"], detail=kw.get("detail", "summary"), task_id=kw.get("task_id")
        ),
    )
    install_public_output_contract(
        mcp,
        "message",
        MessageOutput,
        lambda raw, kw: message_result(
            raw,
            sender=kw["sender"],
            text=kw.get("text"),
            target=kw.get("target"),
            message_hash=kw.get("message_hash"),
            mode=kw.get("mode"),
            require_reply=kw.get("require_reply", False),
            alert=kw.get("alert", False),
            show_all=kw.get("history", kw.get("show_all", False)),
        ),
    )
    install_public_output_contract(
        mcp,
        "task",
        TaskOutput,
        lambda raw, kw: task_result(raw, task_request_action(kw["boundary"])),
    )
    install_public_output_contract(
        mcp, "cmd", CmdOutput, lambda raw, kw: cmd_result(raw, kw["request"].action)
    )
    install_public_output_contract(
        mcp, "context", ContextOutput, lambda raw, kw: context_result(raw, kw["request"].action)
    )
    install_public_output_contract(mcp, "health", HealthOutput, lambda raw, kw: health_result(raw))

    return mcp

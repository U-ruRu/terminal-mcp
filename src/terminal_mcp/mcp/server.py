# ruff: noqa: E501
from typing import Annotated
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from terminal_mcp.api_models import (
    AgentFinishResponse,
    AgentOverviewResponse,
    CancelResponse,
    CoordinateResponse,
    HealthResponse,
    MessageResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
)
from terminal_mcp.core.orchestration import public_agent_name
from terminal_mcp.core.service import DEFAULT_READ_LINES, MAX_READ_LINES
from terminal_mcp.telemetry import observed

_SAFE_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_SAFE_OPERATION = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
ScopeItem = Annotated[str, Field(min_length=1, max_length=80)]
StepItem = Annotated[str, Field(min_length=1, max_length=160)]


def _structured_result(data, summary: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=data.model_dump(mode="json", exclude_none=True),
        isError=False,
    )


def _overview_summary(data: AgentOverviewResponse) -> str:
    if data.registration_required:
        return "Agent session expired. Call agent_start."
    identity = (
        (data.self.agent_id or data.self.name)
        if data.self
        else data.agent_name or "unknown"
    )
    return f"{identity} | active={len(data.active)} | overlaps={len(data.overlaps)}"


def build_mcp(service, public_base_url: str = "http://127.0.0.1:8080", auth_mode: str = "none"):
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

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Register an agent with a concrete step plan. details is required for new registration; work_scope is optional. Pass an existing full agent_id to revise that session plan without allocating a new identity.",
    )
    async def agent_start(
        task_summary: Annotated[str | None, Field(min_length=1, max_length=120)] = None,
        intent: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
        details: Annotated[list[StepItem] | None, Field(min_length=1, max_length=12)] = None,
        work_scope: Annotated[list[ScopeItem] | None, Field(max_length=4)] = None,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(
                service,
                "mcp",
                "agent_start",
                service.agent_start(
                    task_summary=task_summary,
                    intent=intent,
                    details=details,
                    work_scope=work_scope,
                    agent_id=agent_id,
                ),
            )
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Inspect or update current coordination. agent_id alone returns current step/intent/detail. step selects a plan step. Providing step+intent updates current work and refreshes the 180-second task lease. show_details includes other agents' registered plans. Always inspect pending_messages and acknowledge them with message before new run work.",
    )
    async def coordinate(
        agent_id: str,
        step: Annotated[int | None, Field(ge=1)] = None,
        intent: Annotated[str | None, Field(min_length=1, max_length=160)] = None,
        show_details: bool = False,
    ) -> Annotated[CallToolResult, CoordinateResponse]:
        data = CoordinateResponse.model_validate(
            await observed(
                service,
                "mcp",
                "coordinate",
                service.coordinate(agent_id, step=step, intent=intent, show_details=show_details),
            )
        )
        return _structured_result(
            data,
            f"{data.agent_name} step={data.step} — {data.intent}" if data.ok else f"Coordinate failed: {data.error}",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Send or acknowledge coordination messages. To send, provide text and optionally target using the public agent name only; omit target for broadcast to all currently active peers. To acknowledge, provide message_hash instead of text/target. Returns recipients/read receipts.",
    )
    async def message(
        agent_id: str,
        text: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
        target: Annotated[str | None, Field(min_length=1, max_length=64)] = None,
        message_hash: Annotated[str | None, Field(min_length=8, max_length=8)] = None,
    ) -> Annotated[CallToolResult, MessageResponse]:
        data = MessageResponse.model_validate(
            await observed(
                service,
                "mcp",
                "message",
                service.message(agent_id, text=text, target=target, message_hash=message_hash),
            )
        )
        summary = (
            f"Message {data.message_hash}: read_by={','.join(data.read_by) or '-'}"
            if message_hash
            else f"Message {data.message_hash}: delivered_to={','.join(data.delivered_to) or '-'}"
        )
        return _structured_result(data, summary if data.ok else f"Message failed: {data.error}")

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Show active agent sessions and coordination detail. Also inspect pending_messages and acknowledge unread coordination mail before starting new run work.",
    )
    async def agents(agent_id: str) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(service, "mcp", "agents", service.agents(agent_id))
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Finish an agent session early. Sessions also expire automatically after inactivity.",
    )
    async def agent_finish(agent_id: str) -> Annotated[CallToolResult, AgentFinishResponse]:
        data = AgentFinishResponse.model_validate(
            await observed(service, "mcp", "agent_finish", service.agent_finish(agent_id))
        )
        return _structured_result(
            data,
            f"{data.agent_name} finished."
            if data.ok
            else "Agent session expired. Call agent_start.",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Queue a shell command for FIFO execution. Requires an active session, a task context refreshed within 180 seconds, and no unread coordination messages. If pending_messages is non-empty, acknowledge each with message(agent_id, message_hash=...) and coordinate any changed work before retrying run.",
    )
    async def run(agent_id: str, cmd: str) -> Annotated[CallToolResult, RunResponse]:
        data = RunResponse.model_validate(
            await observed(service, "mcp", "run", service.run(cmd, agent_id=agent_id))
        )
        summary = (
            f"Command {data.cmd_hash} queued."
            if data.ok
            else (
                "Task context expired. Call coordinate with step and intent before run."
                if data.task_context_expired
                else (
                    "Agent session expired. Call agent_start."
                    if data.registration_required
                    else f"Command was not queued: {data.error}"
                )
            )
        )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Run one persisted emergency command outside FIFO. agent_id is optional and used for attribution; when supplied, response also surfaces pending_messages without blocking recovery.",
    )
    async def recovery(
        cmd: str,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, RecoveryResponse]:
        raw = await observed(service, "mcp", "recovery", service.recovery(cmd, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = RecoveryResponse.model_validate(raw)
        return _structured_result(
            data,
            f"Recovery {data.cmd_hash or 'unallocated'} returned {data.displayed_lines_count} line(s).",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Read terminal output. Provide agent_id and cmd_hash together for one command, or omit both for the global stream. Agent-bound reads include compact active_agents and pending_messages; acknowledge unread coordination messages before new run work.",
    )
    async def read(
        agent_id: str | None = None,
        cmd_hash: str | None = None,
        lines_count: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = DEFAULT_READ_LINES,
        offset: int | None = None,
    ) -> Annotated[CallToolResult, ReadResponse]:
        if bool(agent_id) != bool(cmd_hash):
            awareness = await service.awareness(agent_id)
            data = ReadResponse(
                ok=False,
                lines=[],
                next_offset=0,
                overall_lines_count=None,
                displayed_lines_count=0,
                cmd_hash=cmd_hash,
                status=None,
                exit_code=None,
                error="read.scope: provide agent_id and cmd_hash together, or omit both for global terminal",
                **awareness,
            )
        else:
            raw = await observed(
                service,
                "mcp",
                "read",
                service.read(cmd_hash, lines_count, offset, agent_id=agent_id),
            )
            raw.pop("agent_id", None)
            raw["agent_name"] = public_agent_name(agent_id)
            data = ReadResponse.model_validate(raw)
        scope = f"command {cmd_hash}" if cmd_hash else "global log"
        return _structured_result(
            data,
            (
                f"Returned {data.displayed_lines_count} line(s) from {scope}."
                if data.ok
                else f"Read failed: {data.error}"
            ),
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Cancel a queued or running command. agent_id is optional; when supplied, response also surfaces pending_messages. Use read for final command status.",
    )
    async def cancel(
        cmd_hash: str,
        agent_id: str | None = None,
    ) -> Annotated[CallToolResult, CancelResponse]:
        raw = await observed(service, "mcp", "cancel", service.cancel(cmd_hash, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = CancelResponse.model_validate(raw)
        summary = (
            f"Command {data.cmd_hash} was cancelled."
            if data.ok
            else f"Command {data.cmd_hash} was not cancelled: {data.error}"
        )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Return terminal service health. agent_id is optional; when supplied for a live session it refreshes session TTL and includes pending coordination messages.",
    )
    async def health(agent_id: str | None = None) -> Annotated[CallToolResult, HealthResponse]:
        raw = await observed(service, "mcp", "health", service.health(auth_mode, agent_id=agent_id))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(agent_id)
        data = HealthResponse.model_validate(raw)
        return _structured_result(
            data,
            "Terminal service is healthy." if data.ok else "Terminal service is unhealthy.",
        )

    return mcp

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
    HealthResponse,
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
        description="Start every new work session here. Receive an agent call sign, declare the immediate task and scope, and inspect recent active agents.",
    )
    async def agent_start(
        task_summary: Annotated[str, Field(min_length=1, max_length=120)],
        intent: Annotated[str, Field(min_length=1, max_length=160)],
        work_scope: Annotated[list[ScopeItem], Field(min_length=1, max_length=4)],
    ) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(
                service, "mcp", "agent_start", service.agent_start(task_summary, intent, work_scope)
            )
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description="Refresh the task lease with one short intent, maximum 160 characters. The work_scope declared at agent_start is preserved.",
    )
    async def agent_task(
        agent_id: str,
        intent: Annotated[str, Field(min_length=1, max_length=160)],
    ) -> Annotated[CallToolResult, AgentOverviewResponse]:
        data = AgentOverviewResponse.model_validate(
            await observed(
                service,
                "mcp",
                "agent_task",
                service.agent_task(agent_id, intent),
            )
        )
        return _structured_result(data, _overview_summary(data))

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description="Show active agent sessions and coordination detail. Runtime run/read responses use compact one-line agent awareness to save context.",
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
        description="Queue a shell command for FIFO execution. Requires an active agent session and a task context refreshed within 180 seconds. Response includes active_agents with public names and each peer's latest command hash/timestamp.",
    )
    async def run(agent_id: str, cmd: str) -> Annotated[CallToolResult, RunResponse]:
        data = RunResponse.model_validate(
            await observed(service, "mcp", "run", service.run(cmd, agent_id=agent_id))
        )
        summary = (
            f"Command {data.cmd_hash} queued."
            if data.ok
            else (
                "Task context expired. Call agent_task before run."
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
        description="Run one persisted emergency command outside FIFO. agent_id is optional and used only for attribution when available.",
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
        description="Read terminal output. Provide agent_id and cmd_hash together for one command, or omit both for the global stream. active_agents is a compact string array with time, public name, command/event and intent.",
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
        description="Cancel a queued or running command. agent_id is optional and used only as caller metadata; use read for final status.",
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
        description="Return terminal service health. No agent session is required.",
    )
    async def health() -> Annotated[CallToolResult, HealthResponse]:
        raw = await observed(service, "mcp", "health", service.health(auth_mode))
        raw.pop("agent_id", None)
        raw["agent_name"] = public_agent_name(None)
        data = HealthResponse.model_validate(raw)
        return _structured_result(
            data,
            "Terminal service is healthy." if data.ok else "Terminal service is unhealthy.",
        )

    return mcp

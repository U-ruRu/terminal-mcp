# ruff: noqa: E501
from typing import Annotated
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from terminal_mcp.api_models import (
    CancelResponse,
    HealthResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
)
from terminal_mcp.core.service import DEFAULT_READ_LINES, MAX_READ_LINES

_SAFE_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_SAFE_OPERATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _structured_result(data, summary: str, exclude_none: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=data.model_dump(mode="json", exclude_none=exclude_none),
        isError=False,
    )


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
        description=(
            "Queue a shell command for FIFO execution. Returns after persistence and enqueue, "
            "or after the fixed 45-second timeout. Response fields: ok, cmd_hash, error."
        ),
    )
    async def run(cmd: str) -> Annotated[CallToolResult, RunResponse]:
        data = RunResponse.model_validate(await service.run(cmd))
        summary = (
            f"Command {data.cmd_hash} queued."
            if data.ok
            else f"Command was not queued: {data.error}"
        )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description=(
            "Run one persisted emergency shell command immediately outside FIFO. "
            "Waits for completion or the fixed 45-second timeout, returns up to 500 lines, "
            "and keeps full output readable by cmd_hash."
        ),
    )
    async def recovery(cmd: str) -> Annotated[CallToolResult, RecoveryResponse]:
        data = RecoveryResponse.model_validate(await service.recovery(cmd))
        return _structured_result(
            data,
            f"Recovery command {data.cmd_hash or 'unallocated'} returned "
            f"{data.displayed_lines_count} line(s).",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description=(
            "Read stored terminal output. Returns 500 latest lines by default and never more than 1000. "
            "Negative offsets count from the end. Command status is returned only here."
        ),
    )
    async def read(
        cmd_hash: str | None = None,
        lines_count: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = DEFAULT_READ_LINES,
        offset: int | None = None,
    ) -> Annotated[CallToolResult, ReadResponse]:
        data = ReadResponse.model_validate(await service.read(cmd_hash, lines_count, offset))
        scope = f"command {cmd_hash}" if cmd_hash else "global log"
        return _structured_result(
            data,
            f"Returned {data.displayed_lines_count} line(s) from {scope}.",
        )

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_OPERATION,
        description=(
            "Cancel a queued or running command by its eight-character cmd_hash. "
            "Response fields: ok, cmd_hash, error. Use read for final status."
        ),
    )
    async def cancel(cmd_hash: str) -> Annotated[CallToolResult, CancelResponse]:
        data = CancelResponse.model_validate(await service.cancel(cmd_hash))
        summary = (
            f"Command {data.cmd_hash} was cancelled."
            if data.ok
            else f"Command {data.cmd_hash} was not cancelled: {data.error}"
        )
        return _structured_result(data, summary)

    @mcp.tool(
        structured_output=True,
        annotations=_SAFE_READ_ONLY,
        description=(
            "Return application, storage, authentication interface, FIFO scheduler, queue, terminal user, "
            "working directory and privilege health information."
        ),
    )
    async def health() -> Annotated[CallToolResult, HealthResponse]:
        data = HealthResponse.model_validate(await service.health(auth_mode))
        return _structured_result(
            data,
            "Terminal service is healthy." if data.ok else "Terminal service is unhealthy.",
            exclude_none=True,
        )

    return mcp

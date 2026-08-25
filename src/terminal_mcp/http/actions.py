# ruff: noqa: E501
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from terminal_mcp.api_models import (
    CancelResponse,
    HealthResponse,
    ReadResponse,
    RecoveryResponse,
    RunResponse,
)
from terminal_mcp.core.service import DEFAULT_READ_LINES, MAX_READ_LINES


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(StrictRequest):
    cmd: str = Field(min_length=1, description="Shell script passed to /bin/bash -s through stdin.")


class ReadRequest(StrictRequest):
    cmd_hash: str | None = Field(
        default=None,
        description="Eight-character command identifier. Omit for the global terminal log.",
    )
    lines_count: int = Field(
        default=DEFAULT_READ_LINES,
        ge=1,
        le=MAX_READ_LINES,
        description="Maximum number of lines returned.",
    )
    offset: int | None = Field(
        default=None,
        description="Zero-based start index. Negative values count from the end. When omitted, the latest lines are returned.",
    )


class RecoveryRequest(StrictRequest):
    cmd: str = Field(
        min_length=1,
        description="Emergency shell script executed immediately outside FIFO, persisted, with a fixed 45-second timeout.",
    )


class CancelRequest(StrictRequest):
    cmd_hash: str = Field(
        min_length=8, max_length=8, description="Eight-character command identifier."
    )


def build_actions_router(service, auth_mode="none"):
    router = APIRouter(prefix="/actions", tags=["terminal-actions"])

    @router.post(
        "/run",
        operation_id="runCommand",
        summary="Queue a shell command",
        description="Queues a command and returns after it is persisted and added to FIFO, or after the fixed 45-second operation timeout.",
        response_model=RunResponse,
    )
    async def run_command(body: RunRequest):
        return await service.run(body.cmd)

    @router.post(
        "/recovery",
        operation_id="recoveryCommand",
        summary="Run an emergency command outside FIFO",
        description="Executes one persisted shell command immediately outside FIFO. The client waits for completion or the fixed 45-second timeout. At most 500 output lines are returned; full output is available through read.",
        response_model=RecoveryResponse,
    )
    async def recovery_command(body: RecoveryRequest):
        return await service.recovery(body.cmd)

    @router.post(
        "/read",
        operation_id="readTerminal",
        summary="Read terminal output",
        description="Returns at most 1000 lines, 500 by default. Without offset, returns the latest lines. Negative offsets count from the end. Command status is available only here.",
        response_model=ReadResponse,
    )
    async def read_terminal(body: ReadRequest):
        return await service.read(body.cmd_hash, body.lines_count, body.offset)

    @router.get("/read", include_in_schema=False)
    async def read_terminal_compat(
        cmd_hash: str | None = None,
        lines_count: int = DEFAULT_READ_LINES,
        offset: int | None = None,
    ):
        return await service.read(cmd_hash, lines_count, offset)

    @router.post(
        "/cancel",
        operation_id="cancelCommand",
        summary="Cancel a queued or running command",
        description="Removes a queued command or stops a running process. Read the final command status through read.",
        response_model=CancelResponse,
    )
    async def cancel_command(body: CancelRequest):
        return await service.cancel(body.cmd_hash)

    @router.get(
        "/health",
        operation_id="getTerminalHealth",
        summary="Get application and terminal health",
        response_model=HealthResponse,
        response_model_exclude_none=True,
    )
    async def terminal_health():
        return await service.health(auth_mode)

    return router

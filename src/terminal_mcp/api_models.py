from typing import Literal

from pydantic import BaseModel

CommandStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "not_found",
]


class RunResponse(BaseModel):
    ok: bool
    cmd_hash: str | None
    error: str | None


class ReadResponse(BaseModel):
    ok: bool
    lines: list[str]
    next_offset: int
    overall_lines_count: int | None
    displayed_lines_count: int
    cmd_hash: str | None
    status: CommandStatus | None
    exit_code: int | None
    error: str | None


class RecoveryResponse(BaseModel):
    ok: bool
    cmd_hash: str | None
    lines: list[str]
    overall_lines_count: int
    displayed_lines_count: int
    exit_code: int | None
    error: str | None
    duration_ms: int


class HealthCommandResult(BaseModel):
    command: str
    lines: list[str]
    status: CommandStatus
    exit_code: int | None = None
    error: str | None = None
    ok: bool
    duration_ms: int


class CancelResponse(BaseModel):
    ok: bool
    cmd_hash: str
    error: str | None


class TerminalHealth(BaseModel):
    ok: bool
    user: str
    uid: int
    gid: int
    cwd: str
    privilege: str
    shell: str
    terminal_user: str
    scheduler: str
    parallelism: int
    queue_size: int
    running_commands: list[str]


class HealthResponse(BaseModel):
    ok: bool
    application: str
    storage: str
    auth_mode: str
    terminal: TerminalHealth
    custom_command: HealthCommandResult | None = None

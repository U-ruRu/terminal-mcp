"""Versioned process-execution port; deliberately no queues, stores or transports.

The application commits a durable running claim before ``spawn`` and validates
its fence before ``write_stdin``. Replaying an active spawn/input is idempotent;
a released/lost execution MUST NOT be replayed without application arbitration.
PID is diagnostic information, not an authorization token. Adapters must only
signal handles they own, including when a PID is reused after process loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, runtime_checkable

EXECUTION_PORT_VERSION = 1
MAX_EXECUTION_READ_BYTES = 65536


class ExecutionPortError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    execution_id: str
    version: int = EXECUTION_PORT_VERSION

    def __post_init__(self):
        if type(self.version) is not int or self.version != EXECUTION_PORT_VERSION:
            raise ExecutionPortError("execution_version_unsupported")
        if not isinstance(self.execution_id, str) or not 1 <= len(self.execution_id) <= 128:
            raise ExecutionPortError("execution_id_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    execution_id: str
    token: str
    pid: int
    version: int = EXECUTION_PORT_VERSION


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    state: Literal["running", "exited"]
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class CommandExecutionSnapshot:
    command_hash: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    queue_id: int | None
    queue_position: int | None
    execution_started: bool
    exit_code: int | None


class CaptureResult(TypedDict):
    lines: list[str]
    status: str
    exit_code: int | None
    error: str | None
    ok: bool
    duration_ms: int


@runtime_checkable
class ExecutionPort(Protocol):
    version: int

    async def start(self) -> None: ...
    async def spawn(self, request: ExecutionRequest) -> ExecutionHandle: ...
    async def write_stdin(self, handle: ExecutionHandle, command: str) -> None: ...
    async def read_output(self, handle: ExecutionHandle, max_bytes: int) -> bytes: ...
    async def status(self, handle: ExecutionHandle) -> ExecutionStatus: ...
    async def wait(self, handle: ExecutionHandle, timeout_seconds: float | None = None) -> int: ...
    async def signal(
        self, handle: ExecutionHandle, action: Literal["terminate", "kill"]
    ) -> None: ...
    async def terminate(self, handle: ExecutionHandle, grace_seconds: float = 1.0) -> bool: ...
    async def release(self, handle: ExecutionHandle) -> None: ...
    async def process_exists(self, pid: int) -> bool: ...
    async def close(self) -> None: ...
    async def health(self) -> dict: ...
    async def capture(
        self,
        command: str,
        timeout_ms: int = 5000,
        max_output_lines: int = 1000,
        timeout_error: str = "capture timed out",
    ) -> CaptureResult: ...

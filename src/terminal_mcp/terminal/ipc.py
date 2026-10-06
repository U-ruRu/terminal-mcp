"""Restricted Unix IPC adapter; this process never launches or signals a shell."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import secrets
from dataclasses import asdict

from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    ExecutionHandle,
    ExecutionPortError,
    ExecutionStatus,
)

MAX_FRAME_BYTES = 1024 * 1024


def encode_frame(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME_BYTES:
        raise ExecutionPortError("execution_frame_too_large")
    return data + b"\n"


async def read_frame(reader):
    try:
        data = await reader.readline()
        if not data:
            raise EOFError("executor disconnected")
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, asyncio.LimitOverrunError) as exc:
        raise ExecutionPortError("execution_frame_invalid") from exc


class UnixExecutionAdapter:
    version = EXECUTION_PORT_VERSION
    reconnectable = True

    def __init__(self, socket_path, *, rpc_timeout=10.0):
        self.socket_path = str(socket_path)
        self.rpc_timeout = float(rpc_timeout)
        if not math.isfinite(self.rpc_timeout) or self.rpc_timeout <= 0:
            raise ValueError("rpc_timeout must be positive and finite")
        self.client_id = secrets.token_hex(24)
        self._generation = None
        self._offsets = {}
        self._read_locks = {}
        self._closed = False

    async def _rpc(self, op, **args):
        if self._closed:
            raise ExecutionPortError("execution_port_closed")
        request_id = secrets.token_hex(24)
        request = encode_frame(
            {
                "version": self.version,
                "client": self.client_id,
                "id": request_id,
                "op": op,
                "args": args,
            }
        )
        request_generation = self._generation
        for attempt in range(2):
            writer = None
            try:
                budget = self.rpc_timeout
                if op == "capture" and type(args.get("timeout_ms")) is int:
                    budget = max(budget, min(3600, max(0, args["timeout_ms"]) / 1000) + 2)
                async with asyncio.timeout(budget):
                    reader, writer = await asyncio.open_unix_connection(
                        self.socket_path, limit=MAX_FRAME_BYTES + 1
                    )
                    writer.write(request)
                    await writer.drain()
                    response = await read_frame(reader)
                if type(response.get("version")) is not int or response["version"] != self.version:
                    raise ExecutionPortError("execution_version_unsupported")
                if response.get("ok") is not True:
                    error = response.get("error", "execution_protocol_error")
                    if error == "execution_handshake_required" and op != "hello":
                        await self._rpc("hello")
                        if (
                            request_generation is not None
                            and request_generation != self._generation
                        ):
                            # The old operation may already have caused a side effect. Never
                            # replay it across an executor boot, even for diagnostic capture.
                            raise ExecutionPortError("execution_executor_restarted")
                        return await self._rpc(op, **args)
                    raise ExecutionPortError(error)
                result = response.get("result")
                if op == "hello":
                    self._generation = result["generation"]
                return result
            except asyncio.CancelledError:
                if op == "capture":
                    try:
                        await self._rpc("cancel_request", request_id=request_id)
                    except Exception:
                        pass
                raise
            except (OSError, TimeoutError, EOFError) as exc:
                if attempt:
                    raise ExecutionPortError("execution_unavailable") from exc
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass

    async def start(self):
        self._closed = False
        await self._rpc("hello")

    async def spawn(self, request):
        result = await self._rpc("spawn", request=asdict(request))
        return ExecutionHandle(**result)

    async def recover(self, execution_id):
        """Look up an existing execution, NEVER spawn a replacement on loss."""
        result = await self._rpc("recover", execution_id=execution_id)
        if result is None:
            return None
        handle = ExecutionHandle(**result)
        self._offsets[handle.token] = 0
        return handle

    async def input_written(self, handle):
        return bool(await self._rpc("input_written", handle=asdict(handle)))

    async def write_stdin(self, handle, command):
        await self._rpc("write_stdin", handle=asdict(handle), command=command)

    async def read_output(self, handle, max_bytes=MAX_EXECUTION_READ_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EXECUTION_READ_BYTES:
            raise ExecutionPortError("execution_read_bound_invalid")
        lock = self._read_locks.setdefault(handle.token, asyncio.Lock())
        async with lock:
            while True:
                offset = self._offsets.get(handle.token, 0)
                result = await self._rpc(
                    "read_output", handle=asdict(handle), offset=offset, max_bytes=max_bytes
                )
                data = base64.b64decode(result["data"], validate=True)
                if len(data) > max_bytes:
                    raise ExecutionPortError("execution_protocol_error")
                self._offsets[handle.token] = offset + len(data)
                if data or result["eof"]:
                    return data
                await asyncio.sleep(0.01)

    async def output_truncated(self, handle):
        return bool(await self._rpc("output_truncated", handle=asdict(handle)))

    async def status(self, handle):
        return ExecutionStatus(**await self._rpc("status", handle=asdict(handle)))

    async def wait(self, handle, timeout_seconds=None):
        # Polling keeps each IPC operation bounded; cancellation never abandons a server wait.
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds < 0
        ):
            raise ExecutionPortError("execution_timeout_invalid")
        async with asyncio.timeout(timeout_seconds):
            while True:
                status = await self.status(handle)
                if status.state == "exited":
                    return status.exit_code
                await asyncio.sleep(0.01)

    async def signal(self, handle, action):
        await self._rpc("signal", handle=asdict(handle), action=action)

    async def terminate(self, handle, grace_seconds=1.0):
        return await self._rpc("terminate", handle=asdict(handle), grace_seconds=grace_seconds)

    async def release(self, handle):
        await self._rpc("release", handle=asdict(handle))
        self._offsets.pop(handle.token, None)
        self._read_locks.pop(handle.token, None)

    async def process_exists(self, pid):
        return await self._rpc("process_exists", pid=pid)

    async def close(self):
        # Detach only. The separately managed executor owns the process lifetime.
        self._closed = True
        self._offsets.clear()
        self._read_locks.clear()

    async def health(self):
        return await self._rpc("health")

    async def capture(
        self, command, timeout_ms=5000, max_output_lines=1000, timeout_error="capture timed out"
    ):
        return await self._rpc(
            "capture",
            command=command,
            timeout_ms=timeout_ms,
            max_output_lines=max_output_lines,
            timeout_error=timeout_error,
        )

"""Process-only executor with Linux credential-restricted, versioned Unix IPC.

No repository, durable queue, OAuth, HTTP, MCP, Console or Fleet listener lives here.
The bounded output spool is disposable, survives API restarts, and is lost with this
service. Run under systemd KillMode=control-group so service loss also kills descendants.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import signal
import socket
import stat
import struct
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    ExecutionHandle,
    ExecutionPortError,
    ExecutionRequest,
)
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter
from terminal_mcp.terminal.ipc import MAX_FRAME_BYTES, encode_frame, read_frame


@dataclass
class _Spool:
    handle: ExecutionHandle
    data: bytearray = field(default_factory=bytearray)
    eof: bool = False
    truncated: bool = False
    task: asyncio.Task | None = None


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ExecutionPortError("execution_argument_invalid")
    return value


def _text(value, limit=512 * 1024):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= limit:
        raise ExecutionPortError("execution_argument_invalid")
    return value


def _duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 5:
        raise ExecutionPortError("execution_argument_invalid")
    return value


class ExecutorService:
    def __init__(
        self,
        socket_path,
        *,
        allowed_uids,
        shell="/bin/bash",
        cwd="/",
        user="root",
        grace=1.0,
        spool_max_bytes=16 * 1024 * 1024,
        max_executions=16,
    ):
        self.path = Path(socket_path)
        self.allowed_uids = frozenset(allowed_uids)
        if not self.allowed_uids or any(
            type(uid) is not int or uid < 0 for uid in self.allowed_uids
        ):
            raise ValueError("explicit nonempty allowed_uids is required")
        self.native = InProcessExecutionAdapter(shell, cwd, grace, user)
        self.spool_max_bytes = _integer(spool_max_bytes, 1, 64 * 1024 * 1024)
        self.max_executions = _integer(max_executions, 1, 64)
        self.server = None
        self.client = None
        self.generation = os.urandom(24).hex()
        self.fenced_clients = set()
        self.spools = {}
        self.retired = OrderedDict()
        self.requests = OrderedDict()
        self.spawn_lock = asyncio.Lock()
        self.fence_lock = asyncio.Lock()
        self.connections = set()
        self._socket_inode = None

    async def start(self):
        # A root-owned non-writable directory prevents socket substitution by API users.
        parent = self.path.parent
        if not self.path.is_absolute() or parent.resolve() != parent or not parent.is_dir():
            raise ExecutionPortError("execution_socket_directory_invalid")
        if parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o022:
            raise ExecutionPortError("execution_socket_directory_unsafe")
        await self._prepare_socket()
        await self.native.start()
        self.server = await asyncio.start_unix_server(
            self._connection, path=str(self.path), limit=MAX_FRAME_BYTES + 1
        )
        os.chmod(self.path, 0o660)
        self._socket_inode = self.path.stat().st_ino

    async def _prepare_socket(self):
        try:
            previous = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
            raise ExecutionPortError("execution_socket_exists")
        try:
            async with asyncio.timeout(0.25):
                _, writer = await asyncio.open_unix_connection(str(self.path))
        except ConnectionRefusedError:
            current = self.path.lstat()
            if current.st_ino == previous.st_ino and stat.S_ISSOCK(current.st_mode):
                self.path.unlink()
                return
        except (OSError, TimeoutError):
            pass
        else:
            writer.close()
            await writer.wait_closed()
        raise ExecutionPortError("execution_socket_exists")

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        operations = [item[1] for item in self.requests.values() if not item[1].done()]
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        await self.native.close()
        tasks = [spool.task for spool in self.spools.values() if spool.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.spools.clear()
        if self._socket_inode is not None:
            try:
                if self.path.lstat().st_ino == self._socket_inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid not in self.allowed_uids:
                raise ExecutionPortError("execution_peer_denied")
            if len(self.connections) > 64:
                raise ExecutionPortError("execution_busy")
            async with asyncio.timeout(10):
                request = await read_frame(reader)
            budget = 10
            if request.get("op") == "capture" and isinstance(request.get("args"), dict):
                timeout_ms = request["args"].get("timeout_ms")
                if type(timeout_ms) is int and 0 <= timeout_ms <= 3_600_000:
                    budget = max(budget, timeout_ms / 1000 + 2)
            async with asyncio.timeout(budget):
                result = await self._request(request)
            response = {"version": EXECUTION_PORT_VERSION, "ok": True, "result": result}
        except ExecutionPortError as exc:
            response = {"version": EXECUTION_PORT_VERSION, "ok": False, "error": exc.code}
        except (ValueError, TypeError, KeyError):
            response = {
                "version": EXECUTION_PORT_VERSION,
                "ok": False,
                "error": "execution_argument_invalid",
            }
        except TimeoutError:
            response = {
                "version": EXECUTION_PORT_VERSION,
                "ok": False,
                "error": "execution_request_timeout",
            }
        except asyncio.CancelledError:
            writer.close()
            raise
        except Exception:
            response = {
                "version": EXECUTION_PORT_VERSION,
                "ok": False,
                "error": "execution_internal_error",
            }
        finally:
            self.connections.discard(task)
        try:
            writer.write(encode_frame(response))
            await writer.drain()
        except (OSError, ExecutionPortError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _request(self, request):
        if set(request) != {"version", "client", "id", "op", "args"}:
            raise ExecutionPortError("execution_argument_invalid")
        if type(request["version"]) is not int or request["version"] != EXECUTION_PORT_VERSION:
            raise ExecutionPortError("execution_version_unsupported")
        client = _text(request["client"], 128)
        request_id = _text(request["id"], 128)
        op = _text(request["op"], 32)
        args = request["args"]
        if not isinstance(args, dict):
            raise ExecutionPortError("execution_argument_invalid")
        if op == "hello":
            async with self.fence_lock:
                if args:
                    raise ExecutionPortError("execution_argument_invalid")
                # Old API generations cannot resume side effects after takeover.
                if client in self.fenced_clients:
                    raise ExecutionPortError("execution_client_fenced")
                if self.client is not None and self.client != client:
                    if len(self.fenced_clients) >= 1024:
                        raise ExecutionPortError("execution_generation_capacity_exceeded")
                    self.fenced_clients.add(self.client)
                self.client = client
                return {"version": EXECUTION_PORT_VERSION, "generation": self.generation}
        if self.client is None:
            raise ExecutionPortError("execution_handshake_required")
        if client != self.client:
            raise ExecutionPortError("execution_client_fenced")
        if op == "cancel_request":
            if set(args) != {"request_id"}:
                raise ExecutionPortError("execution_argument_invalid")
            pending = self.requests.get((client, _text(args["request_id"], 128)))
            if pending and not pending[1].done():
                pending[1].cancel()
                await asyncio.gather(pending[1], return_exceptions=True)
            return None
        key = (client, request_id)
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        cached = self.requests.get(key)
        if cached is not None:
            if cached[0] != digest:
                raise ExecutionPortError("execution_request_conflict")
            return await asyncio.shield(cached[1])
        while len(self.requests) >= 128:
            completed = next((k for k, v in self.requests.items() if v[1].done()), None)
            if completed is None:
                raise ExecutionPortError("execution_busy")
            self.requests.pop(completed)
        task = asyncio.create_task(self._dispatch_fenced(client, op, args))
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self.requests[key] = (digest, task)
        # Retain operation through a broken response connection; retries join the exact same work.
        return await asyncio.shield(task)

    def _handle(self, value):
        if not isinstance(value, dict) or set(value) != {"execution_id", "token", "pid", "version"}:
            raise ExecutionPortError("execution_argument_invalid")
        handle = ExecutionHandle(**value)
        ExecutionRequest(handle.execution_id, handle.version)
        _text(handle.token, 128)
        _integer(handle.pid, 1, 2**31 - 1)
        spool = self.spools.get(handle.execution_id)
        if spool is None or spool.handle != handle:
            raise ExecutionPortError("execution_not_owned")
        return spool

    async def _spawn(self, request):
        async with self.spawn_lock:
            if request.execution_id in self.retired:
                raise ExecutionPortError("execution_released")
            spool = self.spools.get(request.execution_id)
            if spool:
                return spool.handle
            if len(self.spools) >= self.max_executions:
                raise ExecutionPortError("execution_capacity_exceeded")
            handle = await self.native.spawn(request)
            spool = _Spool(handle)
            self.spools[request.execution_id] = spool
            spool.task = asyncio.create_task(self._pump(spool))
            return handle

    async def _pump(self, spool):
        try:
            while True:
                chunk = await self.native.read_output(spool.handle, MAX_EXECUTION_READ_BYTES)
                if not chunk:
                    break
                remaining = self.spool_max_bytes - len(spool.data)
                spool.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    spool.truncated = True
        except asyncio.CancelledError:
            spool.truncated = True
            raise
        except Exception:
            spool.truncated = True
        finally:
            spool.eof = True

    async def _release(self, spool):
        await self.native.release(spool.handle)
        if spool.task and not spool.task.done():
            spool.task.cancel()
        if spool.task:
            await asyncio.gather(spool.task, return_exceptions=True)
        self.spools.pop(spool.handle.execution_id, None)
        self.retired[spool.handle.execution_id] = spool.handle
        while len(self.retired) > 1024:
            self.retired.popitem(last=False)

    async def _dispatch_fenced(self, client, op, args):
        if op == "capture":
            # Capture locks only spawn/stdin admission, never its long process wait.
            return await self._dispatch(op, args, client=client)
        async with self.fence_lock:
            if client != self.client:
                raise ExecutionPortError("execution_client_fenced")
            return await self._dispatch(op, args, client=client)

    async def _dispatch(self, op, args, *, client=None):
        fields = {
            "spawn": {"request"},
            "recover": {"execution_id"},
            "write_stdin": {"handle", "command"},
            "read_output": {"handle", "offset", "max_bytes"},
            "output_truncated": {"handle"},
            "input_written": {"handle"},
            "status": {"handle"},
            "signal": {"handle", "action"},
            "terminate": {"handle", "grace_seconds"},
            "release": {"handle"},
            "process_exists": {"pid"},
            "health": set(),
            "capture": {"command", "timeout_ms", "max_output_lines", "timeout_error"},
        }
        if op not in fields or set(args) != fields[op]:
            raise ExecutionPortError("execution_operation_invalid")
        if op == "spawn":
            request = args["request"]
            if not isinstance(request, dict) or set(request) != {"execution_id", "version"}:
                raise ExecutionPortError("execution_argument_invalid")
            return asdict(await self._spawn(ExecutionRequest(**request)))
        if op == "recover":
            execution_id = ExecutionRequest(args["execution_id"]).execution_id
            spool = self.spools.get(execution_id)
            return asdict(spool.handle) if spool else None
        if op == "health":
            return await self.native.health()
        if op == "process_exists":
            return await self.native.process_exists(_integer(args["pid"], 1, 2**31 - 1))
        if op == "capture":
            return await self._capture(args, client=client)
        if op == "release":
            raw = args["handle"]
            if isinstance(raw, dict) and self.retired.get(raw.get("execution_id")) == (
                ExecutionHandle(**raw)
            ):
                return None
        spool = self._handle(args["handle"])
        handle = spool.handle
        if op == "input_written":
            return self.native._entry(handle).input_state == "written"
        if op == "write_stdin":
            return await self.native.write_stdin(handle, _text(args["command"]))
        if op == "read_output":
            offset = _integer(args["offset"], 0, self.spool_max_bytes)
            size = _integer(args["max_bytes"], 1, MAX_EXECUTION_READ_BYTES)
            if offset > len(spool.data):
                raise ExecutionPortError("execution_output_offset_invalid")
            data = bytes(spool.data[offset : offset + size])
            return {
                "data": base64.b64encode(data).decode(),
                "eof": spool.eof and offset + len(data) == len(spool.data),
            }
        if op == "output_truncated":
            return spool.truncated
        if op == "status":
            return asdict(await self.native.status(handle))
        if op == "signal":
            if args["action"] not in ("terminate", "kill"):
                raise ExecutionPortError("execution_signal_invalid")
            return await self.native.signal(handle, args["action"])
        if op == "terminate":
            return await self.native.terminate(handle, _duration(args["grace_seconds"]))
        if op == "release":
            return await self._release(spool)
        raise ExecutionPortError("execution_operation_invalid")

    async def _capture(self, args, *, client):
        command = _text(args["command"])
        timeout_ms = _integer(args["timeout_ms"], 0, 3_600_000)
        max_lines = _integer(args["max_output_lines"], 0, 1_000_000)
        timeout_error = _text(args["timeout_error"], 1024)
        started = time.monotonic()
        async with self.fence_lock:
            if client != self.client:
                raise ExecutionPortError("execution_client_fenced")
            handle = await self._spawn(ExecutionRequest("capture-" + os.urandom(16).hex()))
            spool = self.spools[handle.execution_id]
            try:
                await self.native.write_stdin(handle, command)
            except BaseException:
                await self._release(spool)
                raise
        error = None
        try:
            try:
                await self.native.wait(handle, timeout_ms / 1000)
            except TimeoutError:
                error = timeout_error
                await self.native.terminate(handle, 0.1)
            try:
                await asyncio.wait_for(asyncio.shield(spool.task), 0.5)
            except TimeoutError:
                spool.truncated = True
            status = await self.native.status(handle)
            output = bytes(spool.data[:65536]).decode(errors="replace")
            if spool.truncated or len(spool.data) > 65536:
                error = error or "capture output truncated"
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            ok = status.exit_code == 0 and error is None
            return {
                "lines": [f"[{stamp}] {line}" for line in output.splitlines()[:max_lines]],
                "status": "completed" if ok else "failed",
                "exit_code": status.exit_code,
                "error": error,
                "ok": ok,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            await self._release(spool)


async def _serve(args):
    service = ExecutorService(
        args.socket,
        allowed_uids=args.allowed_uid,
        shell=args.shell,
        cwd=args.cwd,
        user=args.user,
        grace=args.grace,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    await service.start()
    try:
        await stopped.wait()
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--allowed-uid", action="append", type=int, required=True)
    parser.add_argument("--shell", default="/bin/bash")
    parser.add_argument("--cwd", default="/")
    parser.add_argument("--user", default="root")
    parser.add_argument("--grace", type=float, default=1.0)
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()

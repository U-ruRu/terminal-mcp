"""In-process Linux implementation of the versioned process-only ExecutionPort."""

from __future__ import annotations

import asyncio
import hashlib
import os
import pwd
import secrets
import signal
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    ExecutionHandle,
    ExecutionPortError,
    ExecutionStatus,
)


@dataclass
class _OwnedExecution:
    handle: ExecutionHandle
    process: asyncio.subprocess.Process
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_digest: str | None = None
    input_state: str = "new"


class InProcessExecutionAdapter:
    version = EXECUTION_PORT_VERSION

    def __init__(self, shell, cwd, grace, user="root"):
        self.shell, self.cwd, self.grace, self.user = shell, cwd, grace, user
        self.output_readers = {}
        self.output_transports = {}
        self.capture_processes = set()
        self._owned: dict[str, _OwnedExecution] = {}
        self._ids: dict[str, ExecutionHandle] = {}
        self._completed: OrderedDict[ExecutionHandle, ExecutionStatus] = OrderedDict()
        self._spawn_lock = asyncio.Lock()
        self._closed = False

    async def start(self):
        self._closed = False

    def _entry(self, handle):
        if handle.version != self.version:
            raise ExecutionPortError("execution_version_unsupported")
        entry = self._owned.get(handle.token)
        if entry is None or entry.handle != handle:
            raise ExecutionPortError("execution_not_owned")
        return entry

    async def spawn(self, request):
        if request.version != self.version:
            raise ExecutionPortError("execution_version_unsupported")
        async with self._spawn_lock:
            if self._closed:
                raise ExecutionPortError("execution_port_closed")
            existing = self._ids.get(request.execution_id)
            if existing is not None:
                return existing
            process = await self._spawn_native()
            handle = ExecutionHandle(request.execution_id, secrets.token_hex(16), process.pid)
            self._owned[handle.token] = _OwnedExecution(handle, process)
            self._ids[request.execution_id] = handle
            return handle

    async def write_stdin(self, handle, command):
        entry = self._entry(handle)
        digest = hashlib.sha256(command.encode()).hexdigest()
        async with entry.input_lock:
            if entry.input_digest is not None:
                if entry.input_digest != digest:
                    raise ExecutionPortError("execution_input_conflict")
                if entry.input_state == "written":
                    return
                raise ExecutionPortError("execution_input_uncertain")
            entry.input_digest, entry.input_state = digest, "writing"
            try:
                entry.process.stdin.write(command.encode())
                await entry.process.stdin.drain()
                entry.process.stdin.close()
            except BaseException:
                entry.input_state = "failed"
                raise
            entry.input_state = "written"

    async def read_output(self, handle, max_bytes=MAX_EXECUTION_READ_BYTES):
        entry = self._entry(handle)
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EXECUTION_READ_BYTES:
            raise ExecutionPortError("execution_read_bound_invalid")
        reader = self.output_readers.get(entry.process.pid)
        return b"" if reader is None else await reader.read(max_bytes)

    async def status(self, handle):
        completed = self._completed.get(handle)
        if completed is not None:
            return completed
        code = self._entry(handle).process.returncode
        return ExecutionStatus("running" if code is None else "exited", code)

    async def wait(self, handle, timeout_seconds=None):
        completed = self._completed.get(handle)
        if completed is not None:
            return completed.exit_code
        return await self._wait_root_exit(self._entry(handle).process, timeout_seconds)

    async def signal(self, handle, action):
        process = self._entry(handle).process
        if action not in {"terminate", "kill"}:
            raise ExecutionPortError("execution_signal_invalid")
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM if action == "terminate" else signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def terminate(self, handle, grace_seconds=1.0):
        return await self._terminate_native(self._entry(handle).process, grace_seconds)

    async def release(self, handle):
        entry = self._owned.get(handle.token)
        if entry is None:
            return
        self._entry(handle)
        if entry.process.returncode is None:
            if not await self._terminate_native(entry.process, self.grace):
                raise ExecutionPortError("execution_still_running")
            # A ProcessLookup race can precede the child watcher's exit callback.
            await self._wait_root_exit(entry.process, 1.0)
        self._completed[handle] = ExecutionStatus("exited", entry.process.returncode)
        while len(self._completed) > 256:
            self._completed.popitem(last=False)
        self._release_output_stream(entry.process)
        self._owned.pop(handle.token, None)
        if self._ids.get(handle.execution_id) == handle:
            self._ids.pop(handle.execution_id, None)

    async def process_exists(self, pid):
        return self._pid_exists(pid)

    async def close(self):
        async with self._spawn_lock:
            self._closed = True
            handles = [entry.handle for entry in self._owned.values()]
        await asyncio.gather(*(self.release(handle) for handle in handles))
        captures = list(self.capture_processes)
        await asyncio.gather(
            *(self._terminate_native(process, self.grace) for process in captures),
            return_exceptions=True,
        )
        for transport in self.output_transports.values():
            transport.close()
        self.output_readers.clear()
        self.output_transports.clear()

    async def health(self):
        uid = os.geteuid()
        return {
            "user": pwd.getpwuid(uid).pw_name,
            "uid": uid,
            "gid": os.getegid(),
            "cwd": str(self.cwd),
            "privilege": "root" if uid == 0 else "user",
            "shell": self.shell,
            "terminal_user": self.user,
        }

    @staticmethod
    def _pid_exists(pid):
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        return True

    def _drop_privileges(self):
        account = pwd.getpwnam(self.user)

        def drop_privileges():
            if os.geteuid() == 0:
                os.initgroups(account.pw_name, account.pw_gid)
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)

        return drop_privileges

    async def _spawn_native(self, *, capture=False):
        if capture:
            return await asyncio.create_subprocess_exec(
                self.shell,
                "-s",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
                start_new_session=True,
                preexec_fn=self._drop_privileges(),
            )

        read_fd, write_fd = os.pipe()
        read_pipe = os.fdopen(read_fd, "rb", buffering=0)
        write_pipe = os.fdopen(write_fd, "wb", buffering=0)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport = None
        try:
            transport, _ = await loop.connect_read_pipe(lambda: protocol, read_pipe)
            process = await asyncio.create_subprocess_exec(
                self.shell,
                "-s",
                stdin=asyncio.subprocess.PIPE,
                stdout=write_pipe,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
                start_new_session=True,
                preexec_fn=self._drop_privileges(),
            )
        except BaseException:
            if transport is not None:
                transport.close()
            else:
                read_pipe.close()
            raise
        finally:
            write_pipe.close()

        self.output_readers[process.pid] = reader
        self.output_transports[process.pid] = transport
        return process

    async def _wait_root_exit(self, process, timeout_seconds=None):
        if process.returncode is not None:
            return process.returncode

        loop = asyncio.get_running_loop()
        exited = asyncio.Event()
        handle = None

        def check_returncode():
            nonlocal handle
            if process.returncode is not None:
                handle = None
                exited.set()
                return
            handle = loop.call_later(0.01, check_returncode)

        check_returncode()
        try:
            if timeout_seconds is None:
                await exited.wait()
            else:
                await asyncio.wait_for(exited.wait(), max(0.0, timeout_seconds))
            return process.returncode
        finally:
            if handle is not None:
                handle.cancel()

    async def _terminate_native(self, process, grace_seconds=1.0):
        if process.returncode is not None:
            return True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        try:
            await self._wait_root_exit(process, grace_seconds)
            return True
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            try:
                await self._wait_root_exit(process, 1.0)
                return True
            except TimeoutError:
                return False

    def _release_output_stream(self, process):
        if process is None:
            return
        transport = self.output_transports.pop(process.pid, None)
        self.output_readers.pop(process.pid, None)
        if transport is not None:
            transport.close()

    async def capture(
        self, command, timeout_ms=5000, max_output_lines=1000, timeout_error="capture timed out"
    ):
        started = time.monotonic()
        async with self._spawn_lock:
            if self._closed:
                raise ExecutionPortError("execution_port_closed")
            process = await self._spawn_native(capture=True)
            self.capture_processes.add(process)
        communication = asyncio.create_task(process.communicate(command.encode()))
        error = None
        try:
            output, _ = await asyncio.wait_for(
                asyncio.shield(communication), max(0, timeout_ms) / 1000
            )
        except TimeoutError:
            error = timeout_error
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = await communication
        except asyncio.CancelledError:
            await self._terminate_native(process, self.grace)
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise
        finally:
            self.capture_processes.discard(process)
        status = "completed" if process.returncode == 0 and error is None else "failed"
        timestamp = datetime.now(UTC).strftime("%H:%M:%S")
        lines = [
            f"[{timestamp}] {line.rstrip(chr(13))}"
            for line in output.decode(errors="replace").splitlines()[:max_output_lines]
        ]
        return {
            "lines": lines,
            "status": status,
            "exit_code": process.returncode,
            "error": error,
            "ok": status == "completed",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

"""ExecutionPort v1 contract and application scheduler substitution regressions."""

from __future__ import annotations

import asyncio

import pytest

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    ExecutionHandle,
    ExecutionPortError,
    ExecutionStatus,
)
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository


async def eventually(predicate, budget=5):
    # SQLite status transitions have no awaitable event in the repository port.
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(budget):
        while not await predicate():
            tick = loop.call_later(0.005, changed.set)
            try:
                await changed.wait()
            finally:
                tick.cancel()
                changed.clear()


class MemoryExecutionPort:
    """No subprocesses: proves the application depends only on ExecutionPort."""

    version = EXECUTION_PORT_VERSION

    def __init__(self, *, auto_complete=False):
        self.entries = {}
        self.inputs = []
        self.released = []
        self.terminated = []
        self.auto_complete = auto_complete
        self.spawn_entered = asyncio.Event()
        self.allow_spawn = asyncio.Event()
        self.allow_spawn.set()
        self.closed = False

    async def start(self):
        self.closed = False

    async def spawn(self, request):
        self.spawn_entered.set()
        await self.allow_spawn.wait()
        if request.execution_id not in self.entries:
            handle = ExecutionHandle(
                request.execution_id, request.execution_id, 90_000_000 + len(self.entries)
            )
            self.entries[request.execution_id] = {
                "handle": handle,
                "done": asyncio.Event(),
                "code": None,
                "output": [],
            }
        return self.entries[request.execution_id]["handle"]

    async def write_stdin(self, handle, command):
        self.inputs.append((handle.execution_id, command))
        if self.auto_complete:
            self.finish(handle.execution_id, output=b"memory-output\n")

    def finish(self, execution_id, code=0, output=b"done\n"):
        entry = self.entries[execution_id]
        entry["code"] = code
        if output:
            entry["output"].append(output)
        entry["done"].set()

    async def read_output(self, handle, max_bytes):
        assert 1 <= max_bytes <= MAX_EXECUTION_READ_BYTES
        entry = self.entries[handle.execution_id]
        if entry["output"]:
            return entry["output"].pop(0)
        await entry["done"].wait()
        return entry["output"].pop(0) if entry["output"] else b""

    async def status(self, handle):
        code = self.entries[handle.execution_id]["code"]
        return ExecutionStatus("running" if code is None else "exited", code)

    async def wait(self, handle, timeout_seconds=None):
        async with asyncio.timeout(timeout_seconds):
            await self.entries[handle.execution_id]["done"].wait()
        return self.entries[handle.execution_id]["code"]

    async def signal(self, handle, action):
        self.terminated.append(handle.execution_id)
        self.finish(handle.execution_id, -15 if action == "terminate" else -9, b"")

    async def terminate(self, handle, grace_seconds=1):
        await self.signal(handle, "terminate")
        return True

    async def release(self, handle):
        self.released.append(handle.execution_id)

    async def process_exists(self, pid):
        return any(e["handle"].pid == pid and e["code"] is None for e in self.entries.values())

    async def close(self):
        self.closed = True

    async def health(self):
        return {
            "user": "memory",
            "uid": 123,
            "gid": 123,
            "cwd": "/",
            "privilege": "user",
            "shell": "memory",
            "terminal_user": "memory",
        }

    async def capture(
        self, command, timeout_ms=5000, max_output_lines=1000, timeout_error="capture timed out"
    ):
        return {
            "lines": ["memory"],
            "status": "completed",
            "exit_code": 0,
            "error": None,
            "ok": True,
            "duration_ms": 0,
        }


async def memory_runtime(tmp_path, **kwargs):
    repo = SqliteRepository(tmp_path / "runtime.sqlite3")
    await repo.initialize()
    port = MemoryExecutionPort(**kwargs)
    scheduler = CommandScheduler(repo, port, 0.05, queue_reconcile_sec=0.01)
    service = TerminalService(repo, scheduler, 5000)
    return repo, port, scheduler, service


@pytest.mark.asyncio
async def test_queued_cancellation_finishes_before_execution(tmp_path):
    repo, port, scheduler, service = await memory_runtime(tmp_path)
    command = await repo.create("queued", status="queued", queue_id=1)
    try:
        result = await service.cancel(command.cmd_hash)

        assert result["execution_started"] is False
        assert (await repo.get(command.cmd_hash)).status == "cancelled"
        assert port.inputs == []
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_cancel_between_durable_claim_and_spawn_fences_stdin(tmp_path):
    repo, port, scheduler, service = await memory_runtime(tmp_path)
    port.allow_spawn.clear()
    try:
        request = await service.run("must-never-execute", queue_id=1, task_scope="none")
        await asyncio.wait_for(port.spawn_entered.wait(), 5)
        cancelled = await service.cancel(request["cmd_hash"])
        assert cancelled["ok"] is True
        port.allow_spawn.set()

        async def released():
            return request["cmd_hash"] in port.released

        await eventually(released)
        assert port.inputs == []
        assert (await repo.get(request["cmd_hash"])).status == "cancelled"
    finally:
        port.allow_spawn.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_recovery_timeout_terminates_through_port_and_finalizes(tmp_path):
    repo, port, scheduler, _service = await memory_runtime(tmp_path)
    try:
        command = await repo.create("slow-recovery", status="running")
        await scheduler.recovery(command, timeout_seconds=0.01)
        current = await repo.get(command.cmd_hash)
        assert current.status == "cancelled"
        assert current.error.startswith("recovery.timeout:")
        assert port.terminated == [command.cmd_hash]
        assert port.released == [command.cmd_hash]
        assert not scheduler.processes and not scheduler.execution_done
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_uncertain_executor_preserves_the_running_claim(tmp_path):
    class UncertainPort(MemoryExecutionPort):
        async def write_stdin(self, handle, command):
            raise ExecutionPortError("execution_unavailable")

        async def status(self, handle):
            raise ExecutionPortError("execution_unavailable")

        async def process_exists(self, pid):
            raise ExecutionPortError("execution_unavailable")

        async def release(self, handle):
            raise ExecutionPortError("execution_unavailable")

    repo, _, _, _ = await memory_runtime(tmp_path)
    scheduler = CommandScheduler(repo, UncertainPort(), 0.05)
    current = await repo.create("uncertain", status="running", queue_id=1)

    await scheduler._execute(current, method="run")

    assert (await repo.get(current.cmd_hash)).status == "running"

"""ExecutionPort v1 contract and application scheduler substitution regressions."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from terminal_mcp.application.command_scheduler import CommandScheduler
from terminal_mcp.core.execution import (
    EXECUTION_PORT_VERSION,
    MAX_EXECUTION_READ_BYTES,
    ExecutionHandle,
    ExecutionPort,
    ExecutionPortError,
    ExecutionRequest,
    ExecutionStatus,
)
from terminal_mcp.core.ports import CommandSchedulerPort
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter


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
async def test_four_durable_fifo_queues_and_default_selection_over_memory_port(tmp_path):
    repo, port, scheduler, service = await memory_runtime(tmp_path)
    assert isinstance(port, ExecutionPort)
    assert isinstance(scheduler, CommandSchedulerPort)
    try:
        first = [await service.run(f"head-{q}", queue_id=q, task_scope="none") for q in range(1, 5)]
        second = [await service.run(f"tail-{q}", task_scope="none") for q in range(1, 5)]
        assert [item["queue_id"] for item in second] == [1, 2, 3, 4]

        async def four_started():
            return len(port.inputs) == 4

        await eventually(four_started)
        assert set(h for h, _ in port.inputs) == {item["cmd_hash"] for item in first}
        queued = await scheduler.snapshot(second[0]["cmd_hash"])
        assert queued.status == "queued" and not queued.execution_started
        assert queued.queue_position is not None
        await scheduler.submit(await repo.get(first[0]["cmd_hash"]))
        for item in first:
            port.finish(item["cmd_hash"])

        async def eight_started():
            return len(port.inputs) == 8

        await eventually(eight_started)
        for head, tail in zip(first, second, strict=True):
            order = [h for h, _ in port.inputs]
            assert order.index(head["cmd_hash"]) < order.index(tail["cmd_hash"])
            port.finish(tail["cmd_hash"])

        async def completed():
            return all(
                [(await repo.get(x["cmd_hash"])).status == "completed" for x in first + second]
            )

        await eventually(completed)
        await scheduler.submit(await repo.get(second[0]["cmd_hash"]))
        assert len(port.inputs) == 8
        data = await service.read(second[0]["cmd_hash"])
        assert any(line.endswith("done") for line in data["lines"])
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_queued_cancellation_never_reaches_execution_port(tmp_path):
    repo, port, scheduler, service = await memory_runtime(tmp_path)
    try:
        head = await service.run("head", queue_id=1, task_scope="none")
        tail = await service.run("never", queue_id=1, task_scope="none")
        cancelled = await service.cancel(tail["cmd_hash"])
        assert cancelled["ok"] is True and cancelled["execution_started"] is False
        assert (await repo.get(tail["cmd_hash"])).status == "cancelled"
        assert tail["cmd_hash"] not in port.entries
        assert all(h != tail["cmd_hash"] for h, _ in port.inputs)
        assert head["cmd_hash"] != tail["cmd_hash"]
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
async def test_restart_marks_lost_running_failed_without_replaying_side_effect(tmp_path):
    repo, port, scheduler, _service = await memory_runtime(tmp_path, auto_complete=True)
    lost = await repo.create("old-side-effect", status="running", queue_id=1)
    await repo.set_pid(lost.cmd_hash, 88_000_001)
    queued = await repo.create("surviving-queued", status="queued", queue_id=1)
    try:
        await scheduler.start()

        async def completed():
            return (await repo.get(queued.cmd_hash)).status == "completed"

        await eventually(completed)
        assert (await repo.get(lost.cmd_hash)).status == "failed"
        assert lost.cmd_hash not in port.entries
        assert port.inputs == [(queued.cmd_hash, "surviving-queued")]
    finally:
        await scheduler.stop()


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_execution_protocol_rejects_unsupported_versions(version):
    with pytest.raises(ExecutionPortError, match="execution_version_unsupported"):
        ExecutionRequest("version-test", version=version)


@pytest.mark.parametrize("execution_id", ["", "x" * 129, None])
def test_execution_protocol_rejects_invalid_ids(execution_id):
    with pytest.raises(ExecutionPortError, match="execution_id_invalid"):
        ExecutionRequest(execution_id)


@pytest.mark.asyncio
async def test_native_replayed_spawn_and_input_execute_exactly_once(tmp_path):
    port = InProcessExecutionAdapter("/bin/bash", tmp_path, 0.05)
    try:
        request = ExecutionRequest("same-execution")
        handles = await asyncio.gather(*(port.spawn(request) for _ in range(8)))
        assert len(set(handles)) == 1
        handle = handles[0]
        assert asdict(handle)["version"] == 1
        assert isinstance(port, ExecutionPort)
        command = "printf once >> side-effect; printf 'output\\n'"
        await asyncio.gather(*(port.write_stdin(handle, command) for _ in range(8)))
        assert await port.wait(handle, 5) == 0
        chunks = []
        while chunk := await port.read_output(handle, 65536):
            chunks.append(chunk)
        assert b"".join(chunks) == b"output\n"
        assert (tmp_path / "side-effect").read_text() == "once"
        with pytest.raises(ExecutionPortError, match="execution_input_conflict"):
            await port.write_stdin(handle, "printf twice >> side-effect")
        with pytest.raises(ExecutionPortError, match="execution_not_owned"):
            await port.signal(replace(handle, token="forged"), "kill")
        await port.release(handle)
        await port.release(handle)
        assert await port.wait(handle, 1) == 0
        assert (await port.status(handle)).state == "exited"
        with pytest.raises(ExecutionPortError, match="execution_not_owned"):
            await port.signal(handle, "kill")
        assert port._owned == {} and port._ids == {}
        assert port.output_readers == {} and port.output_transports == {}
    finally:
        await port.close()


@pytest.mark.parametrize("limit", [0, -1, 65537, True, 1.5, "4", None])
@pytest.mark.asyncio
async def test_native_output_reads_require_bounded_integer(limit, tmp_path):
    port = InProcessExecutionAdapter("/bin/bash", tmp_path, 0.05)
    try:
        handle = await port.spawn(ExecutionRequest("read-bound"))
        with pytest.raises(ExecutionPortError, match="execution_read_bound_invalid"):
            await port.read_output(handle, limit)
    finally:
        await port.close()


@pytest.mark.asyncio
async def test_cancelled_capture_does_not_leave_owned_child(tmp_path):
    port = InProcessExecutionAdapter("/bin/bash", tmp_path, 0.05)
    capture = asyncio.create_task(port.capture("sleep 60", timeout_ms=10000))
    try:

        async def spawned():
            return bool(port.capture_processes)

        await eventually(spawned)
        process = next(iter(port.capture_processes))
        capture.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(capture, 3)
        assert process.returncode is not None
        assert not port.capture_processes
    finally:
        await port.close()


def test_process_and_scheduler_boundaries_are_structurally_separate():
    root = Path(__file__).parents[1] / "src/terminal_mcp"
    scheduler = ast.parse((root / "application/command_scheduler.py").read_text())
    adapter = ast.parse((root / "terminal/in_process.py").read_text())
    imports = []
    for node in ast.walk(scheduler):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"create_subprocess_exec", "killpg", "connect_read_pipe"}
    assert not {"os", "pwd", "signal"}.intersection(imports)
    for node in ast.walk(adapter):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "claim_next",
                "finish_running",
                "append_lines",
                "cancel_queued",
            }
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("terminal_mcp.storage")


@pytest.mark.asyncio
async def test_durable_claim_prevents_overlapping_running_command(tmp_path):
    repo, _port, _scheduler, _service = await memory_runtime(tmp_path)
    first = await repo.create("one", status="queued", queue_id=1)
    second = await repo.create("two", status="queued", queue_id=1)
    claimed = await asyncio.gather(repo.claim_next(1), repo.claim_next(1))
    assert sum(row is not None for row in claimed) == 1
    assert next(row for row in claimed if row is not None).cmd_hash == first.cmd_hash
    assert await repo.claim_next(1) is None
    await repo.finish_running(first.cmd_hash, "completed", 0, None)
    assert (await repo.claim_next(1)).cmd_hash == second.cmd_hash


@pytest.mark.asyncio
async def test_mid_execution_process_loss_fails_once_then_unblocks_queue(tmp_path):
    repo, port, scheduler, service = await memory_runtime(tmp_path)
    try:
        first = await service.run("first", queue_id=1, task_scope="none")
        second = await service.run("second", queue_id=1, task_scope="none")

        async def started():
            return bool(port.inputs)

        await eventually(started)
        port.finish(first["cmd_hash"], -9, b"")

        async def next_started():
            return len(port.inputs) == 2

        await eventually(next_started)
        failed = await repo.get(first["cmd_hash"])
        assert failed.status == "failed" and failed.exit_code == -9
        assert [item[0] for item in port.inputs] == [first["cmd_hash"], second["cmd_hash"]]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_uncertain_executor_keeps_durable_queue_blocked_and_cleans_local_waiters(tmp_path):
    class UncertainPort(MemoryExecutionPort):
        async def write_stdin(self, handle, command):
            raise ExecutionPortError("execution_unavailable")

        async def status(self, handle):
            raise ExecutionPortError("execution_unavailable")

        async def process_exists(self, pid):
            raise ExecutionPortError("execution_unavailable")

        async def release(self, handle):
            raise ExecutionPortError("execution_unavailable")

    repo, _port, _scheduler, _service = await memory_runtime(tmp_path)
    port = UncertainPort()
    scheduler = CommandScheduler(repo, port, 0.05)
    first = await repo.create("uncertain", status="running", queue_id=1)
    second = await repo.create("must-not-overlap", status="queued", queue_id=1)
    await scheduler._execute(first, method="run")
    assert (await repo.get(first.cmd_hash)).status == "running"
    assert (await repo.get(first.cmd_hash)).pid is not None
    assert (await repo.get(second.cmd_hash)).status == "queued"
    assert await repo.claim_next(1) is None
    assert not scheduler.processes and not scheduler.execution_done
    assert second.cmd_hash not in port.entries

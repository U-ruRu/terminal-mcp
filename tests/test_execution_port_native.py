"""Native ExecutionPort checks requiring Linux shell process execution."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace

import pytest

from terminal_mcp.core.execution import ExecutionPort, ExecutionPortError, ExecutionRequest
from terminal_mcp.terminal.in_process import InProcessExecutionAdapter


async def eventually(predicate, budget=5):
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

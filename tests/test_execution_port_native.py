"""Native ExecutionPort checks requiring Linux shell process execution."""

from __future__ import annotations

import asyncio

import pytest

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

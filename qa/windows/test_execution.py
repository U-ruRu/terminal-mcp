"""Atomic checks for the native Windows execution adapter."""

import asyncio
import os

import pytest
import win32job

from terminal_mcp.config import Settings
from terminal_mcp.core.execution import ExecutionRequest
from terminal_mcp.platform_windows import process_exists
from terminal_mcp.terminal.composition import build_execution


@pytest.fixture
async def port(tmp_path):
    adapter = build_execution(Settings(_env_file=None, cwd=tmp_path))
    await adapter.start()
    try:
        yield adapter
    finally:
        await adapter.close()


async def until(predicate, budget=8):
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(budget):
        while not predicate():
            tick = loop.call_later(0.02, changed.set)
            try:
                await changed.wait()
            finally:
                tick.cancel()
                changed.clear()


async def test_capture_preserves_unicode_and_native_exit_code(port):
    result = await port.capture("Write-Output 'Привет 世界'; exit 7")

    assert result["exit_code"] == 7
    assert any("Привет 世界" in line for line in result["lines"])


@pytest.fixture
async def child_tree(port, tmp_path):
    handle = await port.spawn(ExecutionRequest("tree"))
    await port.write_stdin(
        handle,
        "$p = Start-Process -FilePath powershell.exe -WindowStyle Hidden -PassThru "
        "-ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep 90'; "
        "$p.Id | Set-Content child; exit 0",
    )
    await until(lambda: (tmp_path / "child").exists() and (tmp_path / "child").stat().st_size)
    child = int((tmp_path / "child").read_text())
    assert process_exists(child)
    assert await port.wait(handle, 8) == 0
    return handle, child


async def test_kill_terminates_owned_child_after_root_exit(port, child_tree):
    handle, child = child_tree

    await port.signal(handle, "kill")

    await until(lambda: not process_exists(child))


async def test_failed_job_assignment_releases_native_resources(port, monkeypatch):
    def fail(*args):
        raise PermissionError("job assignment denied")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", fail)

    with pytest.raises(PermissionError):
        await port.spawn(ExecutionRequest("denied"))

    assert port._owned == {}
    assert port._jobs == {}


async def test_powershell_exception_fails_capture(port):
    result = await port.capture("throw 'failure'")

    assert result["status"] == "failed"
    assert result["exit_code"] != 0


async def test_cancelled_capture_releases_job_and_process(port):
    task = asyncio.create_task(port.capture("Start-Sleep 90", timeout_ms=10000))
    await until(lambda: bool(port.capture_processes))
    pid = next(iter(port.capture_processes)).pid

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process_exists(pid)
    assert port._jobs == {}
    assert port.capture_processes == set()


def test_pid_probe_observes_current_process():
    assert process_exists(os.getpid())

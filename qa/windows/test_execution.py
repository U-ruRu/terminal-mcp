"""Native Windows process, Job Object and scheduler acceptance boundaries."""

import asyncio
import os
from dataclasses import replace

import pytest
import win32con
import win32job
from pydantic import ValidationError

from terminal_mcp.config import Settings
from terminal_mcp.core.execution import ExecutionPortError, ExecutionRequest
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
    assert adapter._jobs == {}


async def output(port, handle):
    chunks = []
    while chunk := await port.read_output(handle):
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


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


async def test_admission_replay_unicode_and_ownership(port, tmp_path):
    handles = await asyncio.gather(*(port.spawn(ExecutionRequest("same")) for _ in range(8)))
    assert len(set(handles)) == 1
    handle = handles[0]
    assert not (tmp_path / "effect").exists()
    command = "Add-Content -LiteralPath effect -Value once; Write-Output 'Привет 世界'; exit 7"
    await asyncio.gather(*(port.write_stdin(handle, command) for _ in range(8)))
    assert await port.wait(handle, 8) == 7
    assert "Привет 世界" in await output(port, handle)
    assert (tmp_path / "effect").read_text().splitlines() == ["once"]
    with pytest.raises(ExecutionPortError, match="execution_input_conflict"):
        await port.write_stdin(handle, "Write-Output twice")
    with pytest.raises(ExecutionPortError, match="execution_not_owned"):
        await port.signal(replace(handle, token="forged"), "kill")
    await port.release(handle)
    assert await port.wait(handle, 1) == 7


@pytest.mark.parametrize("limit", [0, -1, 65537, True, 1.5, "4", None])
async def test_output_bound(port, limit):
    handle = await port.spawn(ExecutionRequest("bound"))
    with pytest.raises(ExecutionPortError, match="execution_read_bound_invalid"):
        await port.read_output(handle, limit)


@pytest.mark.parametrize("action", ["terminate", "kill", "release", "close"])
async def test_cancel_owns_child_tree_even_after_root_exit(port, tmp_path, action):
    handle = await port.spawn(ExecutionRequest("tree"))
    command = (
        "$p = Start-Process -FilePath powershell.exe -WindowStyle Hidden -PassThru "
        "-ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep 90'; "
        "$p.Id | Set-Content child; exit 0"
    )
    await port.write_stdin(handle, command)
    await until(lambda: (tmp_path / "child").exists() and (tmp_path / "child").stat().st_size)
    child = int((tmp_path / "child").read_text())
    assert process_exists(child)
    assert await port.wait(handle, 8) == 0
    if action in {"terminate", "kill"}:
        await port.signal(handle, action)
    elif action == "release":
        await port.release(handle)
    else:
        await port.close()
    await until(lambda: not process_exists(child))


async def test_job_assignment_failure_prevents_input(port, tmp_path, monkeypatch):
    def fail(*args):
        raise PermissionError("job assignment denied")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", fail)
    with pytest.raises(PermissionError):
        await port.spawn(ExecutionRequest("denied"))
    assert port._owned == {} and port._jobs == {}
    assert not (tmp_path / "effect").exists()


@pytest.mark.parametrize("command", ["throw 'failure'", "Write-Error 'failure'", "cmd /c exit 9"])
async def test_powershell_failures_are_failed_capture(port, command):
    result = await port.capture(command)
    assert result["status"] == "failed" and result["exit_code"] != 0


async def test_capture_timeout_and_cancellation_release_jobs(port):
    result = await port.capture("Write-Output before; Start-Sleep 90", timeout_ms=800)
    assert result["status"] == "failed" and result["error"] == "capture timed out"
    assert any("before" in line for line in result["lines"])
    task = asyncio.create_task(port.capture("Start-Sleep 90", timeout_ms=10000))
    await until(lambda: bool(port.capture_processes))
    pid = next(iter(port.capture_processes)).pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not process_exists(pid)
    assert port._jobs == {} and port.capture_processes == set()


def test_pid_probe_preserves_an_unowned_live_process(monkeypatch):
    def forbidden(*args):
        pytest.fail("PID observation reached os.kill")

    monkeypatch.setattr(os, "kill", forbidden)
    assert process_exists(os.getpid())
    assert not process_exists(0)
    assert not process_exists(-1)
    assert not process_exists(0xFFFFFFFF)
    assert win32con.SYNCHRONIZE


def test_windows_topology_rejects_unix_including_bypassed_validation():
    with pytest.raises(ValidationError, match="Windows execution requires in_process"):
        Settings(_env_file=None, execution_mode="unix")
    settings = Settings(_env_file=None).model_copy(update={"execution_mode": "unix"})
    with pytest.raises(ExecutionPortError, match="execution_mode_invalid"):
        build_execution(settings)

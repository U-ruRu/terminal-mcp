import asyncio
import time

import pytest

import terminal_mcp.core.service as service_module
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def create_runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def wait_status(service, cmd_hash, statuses, attempts=300):
    statuses = {statuses} if isinstance(statuses, str) else set(statuses)
    result = None
    for _ in range(attempts):
        result = await service.read(cmd_hash, 1000, 0)
        if result["status"] in statuses:
            return result
        await asyncio.sleep(0.01)
    return result


@pytest.mark.asyncio
async def test_short_hash_and_fifo_queue(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    first = await service.run("sleep 0.2; printf 'first\\n'")
    second = await service.run("printf 'second\\n'")
    assert set(first) == {"ok", "cmd_hash", "error"}
    assert first["ok"] is True and first["error"] is None
    assert len(first["cmd_hash"]) == 8
    assert len(second["cmd_hash"]) == 8

    await asyncio.sleep(0.03)
    assert (await service.read(first["cmd_hash"]))["status"] == "running"
    assert (await service.read(second["cmd_hash"]))["status"] == "queued"
    completed = await wait_status(service, second["cmd_hash"], "completed")
    assert completed["status"] == "completed"

    global_lines = await service.read(None, 10, 0)
    assert global_lines["overall_lines_count"] is None
    assert global_lines["displayed_lines_count"] == 2
    assert global_lines["lines"][0].endswith("first")
    assert global_lines["lines"][1].endswith("second")
    await terminal.stop()


@pytest.mark.asyncio
async def test_cancel_removes_queued_command_before_marking_cancelled(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    busy = await service.run("sleep 0.5")
    queued = await service.run("printf 'must-not-run\\n'")
    result = await service.cancel(queued["cmd_hash"])
    assert result == {"ok": True, "cmd_hash": queued["cmd_hash"], "error": None}
    assert all(item.cmd_hash != queued["cmd_hash"] for item in terminal.queue)

    read = await service.read(queued["cmd_hash"])
    assert read["status"] == "cancelled"
    assert read["lines"] == []
    assert read["ok"] is True
    await service.cancel(busy["cmd_hash"])
    await terminal.stop()


@pytest.mark.asyncio
async def test_cancel_running_process_waits_for_real_stop_and_forces_kill(tmp_path, monkeypatch):
    _, terminal, service = await create_runtime(tmp_path)
    monkeypatch.setattr(service_module, "OPERATION_TIMEOUT_SECONDS", 1.5)
    running = await service.run("trap '' TERM; sleep 30")
    await wait_status(service, running["cmd_hash"], "running")

    started = time.monotonic()
    cancelled = await service.cancel(running["cmd_hash"])
    elapsed = time.monotonic() - started
    assert cancelled["ok"] is True
    assert cancelled["error"] is None
    assert elapsed < 1.7
    assert running["cmd_hash"] not in terminal.processes

    read = await service.read(running["cmd_hash"])
    assert read["status"] == "cancelled"
    assert read["exit_code"] is not None
    assert read["error"] is None
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_bypasses_fifo_is_persisted_and_visible_globally(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    busy = await service.run("sleep 1; printf 'fifo-finished\\n'")
    await wait_status(service, busy["cmd_hash"], "running")

    result = await asyncio.wait_for(service.recovery("printf 'recovery-ready\\n'"), 0.5)
    assert result["ok"] is True
    assert len(result["cmd_hash"]) == 8
    assert result["cmd_hash"] != busy["cmd_hash"]
    assert result["overall_lines_count"] == 1
    assert result["displayed_lines_count"] == 1
    assert result["exit_code"] == 0
    assert result["error"] is None
    assert result["lines"][0].endswith("recovery-ready")
    assert (await service.read(busy["cmd_hash"]))["status"] == "running"

    stored = await service.read(result["cmd_hash"])
    assert stored["status"] == "completed"
    assert stored["lines"] == result["lines"]
    global_lines = await service.read()
    assert any(f"[{result['cmd_hash']}]" in line for line in global_lines["lines"])

    await service.cancel(busy["cmd_hash"])
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_nonzero_exit_is_command_failure_not_plugin_error(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    result = await service.recovery("printf 'command-error\\n' >&2; exit 7")
    assert result["ok"] is True
    assert result["exit_code"] == 7
    assert result["error"] is None
    assert result["lines"][0].endswith("command-error")

    stored = await service.read(result["cmd_hash"])
    assert stored["status"] == "failed"
    assert stored["ok"] is True
    assert stored["error"] is None
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_timeout_stops_process_and_does_not_block_fifo(tmp_path, monkeypatch):
    _, terminal, service = await create_runtime(tmp_path)
    monkeypatch.setattr(service_module, "OPERATION_TIMEOUT_SECONDS", 0.05)
    result = await service.recovery("sleep 1")
    assert result["ok"] is False
    assert result["error"].startswith("recovery.timeout:")
    assert result["cmd_hash"] not in terminal.processes

    stored = await service.read(result["cmd_hash"])
    assert stored["status"] == "cancelled"
    assert stored["ok"] is False
    assert stored["error"] == result["error"]

    queued = await service.run("printf 'fifo-still-works\\n'")
    read = await wait_status(service, queued["cmd_hash"], "completed")
    assert read["lines"][0].endswith("fifo-still-works")
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_returns_last_500_but_persists_all_output(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    result = await service.recovery("seq 1 600")
    assert result["ok"] is True
    assert result["overall_lines_count"] == 600
    assert result["displayed_lines_count"] == 500
    assert result["lines"][0].endswith("101")
    assert result["lines"][-1].endswith("600")

    stored = await service.read(result["cmd_hash"], 1000, 0)
    assert stored["overall_lines_count"] == 600
    assert stored["displayed_lines_count"] == 600
    assert stored["lines"][0].endswith("1")
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_calls_do_not_block_each_other(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    slow = asyncio.create_task(service.recovery("sleep 0.4; printf 'slow\\n'"))
    await asyncio.sleep(0.05)
    fast = await asyncio.wait_for(service.recovery("printf 'independent-recovery\\n'"), 0.25)
    assert fast["ok"] is True
    assert fast["lines"][0].endswith("independent-recovery")
    slow_result = await slow
    assert slow_result["ok"] is True
    assert slow_result["cmd_hash"] != fast["cmd_hash"]
    await terminal.stop()


@pytest.mark.asyncio
async def test_recovery_can_stop_stuck_fifo_process_and_release_queue(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    running = await service.run("printf '%s' $$ > stuck.pgid; sleep 30")
    queued = await service.run("printf 'queue-released\\n'")
    for _ in range(200):
        if (tmp_path / "stuck.pgid").exists():
            break
        await asyncio.sleep(0.01)

    result = await service.recovery("kill -TERM -- -$(cat stuck.pgid); printf 'signal-sent\\n'")
    assert result["ok"] is True
    assert result["lines"][0].endswith("signal-sent")

    released = await wait_status(service, queued["cmd_hash"], "completed")
    assert released["status"] == "completed"
    assert released["lines"][0].endswith("queue-released")
    assert (await service.read(running["cmd_hash"]))["status"] == "failed"
    await terminal.stop()


@pytest.mark.asyncio
async def test_run_restarts_failed_fifo_worker_before_enqueue(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    await terminal.start()
    terminal.worker.cancel()
    try:
        await terminal.worker
    except asyncio.CancelledError:
        pass
    assert terminal.worker.done()

    submitted = await service.run("printf 'worker-restarted\\n'")
    assert submitted["ok"] is True
    completed = await wait_status(service, submitted["cmd_hash"], "completed")
    assert completed["lines"][0].endswith("worker-restarted")
    await terminal.stop()

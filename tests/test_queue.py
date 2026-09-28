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
    first = await service.run("sleep 0.2; printf 'first\\n'", queue_id=1, task_scope="none")
    second = await service.run("printf 'second\\n'", queue_id=1, task_scope="none")
    assert {"ok", "cmd_hash", "error", "queue_id", "queue_position"} <= set(first)
    assert first["queue_id"] == second["queue_id"] == 1
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
    busy = await service.run("sleep 0.5", queue_id=1, task_scope="none")
    await wait_status(service, busy["cmd_hash"], "running")
    queued = await service.run("printf 'must-not-run\\n'", queue_id=1, task_scope="none")
    assert (await service.read(queued["cmd_hash"]))["status"] == "queued"
    result = await service.cancel(queued["cmd_hash"])
    assert result["cancelled_from"] == "queued"
    assert result["execution_started"] is False
    assert result["ok"] is True
    assert result["cmd_hash"] == queued["cmd_hash"]
    assert result["error"] is None
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
    running = await service.run("trap '' TERM; sleep 30", task_scope="none")
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
    busy = await service.run("sleep 1; printf 'fifo-finished\\n'", task_scope="none")
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
    assert any(f" {result['cmd_hash']} " in line for line in global_lines["lines"])

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

    queued = await service.run("printf 'fifo-still-works\\n'", task_scope="none")
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
    running = await service.run("printf '%s' $$ > stuck.pgid; sleep 30", task_scope="none")
    queued = await service.run("printf 'queue-released\\n'", task_scope="none")
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
    worker = terminal.workers[1]
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    assert worker.done()

    submitted = await service.run("printf 'worker-restarted\\n'", queue_id=1, task_scope="none")
    assert submitted["ok"] is True
    completed = await wait_status(service, submitted["cmd_hash"], "completed")
    assert completed["lines"][0].endswith("worker-restarted")
    await terminal.stop()


@pytest.mark.asyncio
async def test_stop_kills_process_that_ignores_sigterm(tmp_path):
    repo = SqliteRepository(tmp_path / "shutdown.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.05)
    process = await terminal._spawn()
    terminal.processes["stubborn"] = process
    process.stdin.write(b"trap '' TERM\nsleep 60\n")
    await process.stdin.drain()
    process.stdin.close()
    await asyncio.sleep(0.05)

    await asyncio.wait_for(terminal.stop(), 1.0)
    assert process.returncode is not None


async def register_queue_agent(service, label):
    result = await service.agent_start(
        task_summary=f"{label} queue test",
        intent="exercise durable queue lifecycle",
        details=["exercise durable queue lifecycle"],
        work_scope=[],
    )
    assert result["ok"] is True
    return result["self"]["agent_id"]


@pytest.mark.asyncio
async def test_queued_command_survives_explicit_agent_finish(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    owner = await register_queue_agent(service, "owner")
    busy = await service.run("sleep 0.2", queue_id=1, task_scope="none")
    await wait_status(service, busy["cmd_hash"], "running")
    durable = await service.run(
        "printf 'survived-finish\\n' > survived-finish.txt",
        agent_id=owner,
        queue_id=1,
        task_scope="none",
    )
    finished = await service.agent_finish(owner)
    assert finished["finished"] is True
    completed = await wait_status(service, durable["cmd_hash"], "completed")
    assert completed["status"] == "completed"
    assert (tmp_path / "survived-finish.txt").read_text() == "survived-finish\n"
    await terminal.stop()


@pytest.mark.asyncio
async def test_running_command_survives_explicit_agent_finish(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    owner = await register_queue_agent(service, "running-owner")
    durable = await service.run(
        "sleep 0.15; printf 'running-survived\\n' > running-survived.txt",
        agent_id=owner,
        queue_id=1,
        task_scope="none",
    )
    await wait_status(service, durable["cmd_hash"], "running")
    finished = await service.agent_finish(owner)
    assert finished["finished"] is True
    completed = await wait_status(service, durable["cmd_hash"], "completed")
    assert completed["status"] == "completed"
    assert (tmp_path / "running-survived.txt").read_text() == "running-survived\n"
    await terminal.stop()


@pytest.mark.asyncio
async def test_queued_command_survives_forced_agent_expiry(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    owner = await register_queue_agent(service, "expired-owner")
    busy = await service.run("sleep 0.2", queue_id=1, task_scope="none")
    await wait_status(service, busy["cmd_hash"], "running")
    durable = await service.run(
        "printf 'survived-expiry\\n' > survived-expiry.txt",
        agent_id=owner,
        queue_id=1,
        task_scope="none",
    )
    await service.agent_coordinator.store.end(
        owner, "forced", "max_session_duration", "2099-01-01T00:00:00.000Z"
    )
    completed = await wait_status(service, durable["cmd_hash"], "completed")
    assert completed["status"] == "completed"
    assert (tmp_path / "survived-expiry.txt").read_text() == "survived-expiry\n"
    await terminal.stop()


@pytest.mark.asyncio
async def test_worker_restart_executes_persisted_command_from_finished_agent(tmp_path):
    repo, terminal, service = await create_runtime(tmp_path)
    owner = await register_queue_agent(service, "restart-owner")
    finished = await service.agent_finish(owner)
    assert finished["finished"] is True
    durable = await repo.create(
        "printf 'restart-survived\\n' > restart-survived.txt",
        agent_id=owner,
        command_type="run",
        command_preview="durable after finish",
        queue_id=1,
    )
    await terminal.start()
    completed = await wait_status(service, durable.cmd_hash, "completed")
    assert completed["status"] == "completed"
    assert (tmp_path / "restart-survived.txt").read_text() == "restart-survived\n"
    await terminal.stop()


@pytest.mark.asyncio
async def test_worker_does_not_wedge_when_output_pipe_never_reaches_eof(tmp_path, monkeypatch):
    _, terminal, service = await create_runtime(tmp_path)
    never = asyncio.Event()

    async def stuck_pipe(_command, _reader):
        await never.wait()

    monkeypatch.setattr(terminal, "_pipe_output", stuck_pipe)
    first = await service.run("true", queue_id=1, task_scope="none")
    second = await service.run("true", queue_id=1, task_scope="none")
    assert (await wait_status(service, first["cmd_hash"], "completed"))["status"] == "completed"
    assert (await wait_status(service, second["cmd_hash"], "completed"))["status"] == "completed"
    assert (await terminal.health())["queues"][0]["queued"] == 0
    await terminal.stop()


@pytest.mark.asyncio
async def test_inherited_output_fd_after_root_exit_does_not_hold_fifo(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    first = await service.run(
        (
            'python3 -c "import os,time; p=os.fork(); '
            'time.sleep(1) if p == 0 else None; os._exit(0)"'
        ),
        queue_id=1,
        task_scope="none",
    )
    second = await service.run(
        "printf 'after-inherited-fd\\n' > after-inherited-fd.txt",
        queue_id=1,
        task_scope="none",
    )
    completed = await asyncio.wait_for(
        wait_status(service, second["cmd_hash"], "completed"), timeout=0.8
    )
    assert completed["status"] == "completed"
    assert (await service.read(first["cmd_hash"]))["output_truncated"] is True
    assert (tmp_path / "after-inherited-fd.txt").read_text() == "after-inherited-fd\n"
    await terminal.stop()


@pytest.mark.asyncio
async def test_silent_live_root_process_is_not_treated_as_wedged(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    command = await service.run(
        "sleep 0.25; printf 'still-alive\\n' > still-alive.txt",
        queue_id=1,
        task_scope="none",
    )
    await wait_status(service, command["cmd_hash"], "running")
    await asyncio.sleep(0.12)
    assert (await service.read(command["cmd_hash"]))["status"] == "running"
    completed = await wait_status(service, command["cmd_hash"], "completed")
    assert completed["status"] == "completed"
    assert (tmp_path / "still-alive.txt").read_text() == "still-alive\n"
    await terminal.stop()

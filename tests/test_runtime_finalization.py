import asyncio

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def create_runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(
        repo,
        "/bin/bash",
        tmp_path,
        0.1,
        queue_reconcile_sec=0.02,
    )
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def wait_for(predicate, attempts=300):
    for _ in range(attempts):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2])
async def test_terminalization_retries_without_killing_worker(tmp_path, monkeypatch, failures):
    repo, terminal, service = await create_runtime(tmp_path)
    original = repo.finish_running
    attempts = {}

    async def flaky(cmd_hash, status, exit_code=None, error=None):
        attempts[cmd_hash] = attempts.get(cmd_hash, 0) + 1
        if attempts[cmd_hash] <= failures:
            raise RuntimeError("injected finalization failure")
        return await original(cmd_hash, status, exit_code, error)

    monkeypatch.setattr(repo, "finish_running", flaky)
    result = await service.run("printf 'done\n'", queue_id=1, task_scope="none")
    cmd_hash = result["cmd_hash"]

    async def completed():
        current = await repo.get(cmd_hash)
        return current if current and current.status == "completed" else None

    current = await wait_for(completed)
    assert current.exit_code == 0
    assert attempts[cmd_hash] == failures + 1
    assert terminal.workers[1].done() is False
    assert cmd_hash not in terminal.finalization_pending
    await terminal.stop()


@pytest.mark.asyncio
async def test_persistent_finalization_is_pending_then_background_reconciles(tmp_path, monkeypatch):
    repo, terminal, service = await create_runtime(tmp_path)
    original = repo.finish_running
    blocked = set()

    async def failing(cmd_hash, status, exit_code=None, error=None):
        if cmd_hash in blocked:
            raise RuntimeError("persistent injected failure")
        return await original(cmd_hash, status, exit_code, error)

    monkeypatch.setattr(repo, "finish_running", failing)
    result = await service.run("printf 'persisted later\n'", queue_id=1, task_scope="none")
    cmd_hash = result["cmd_hash"]
    blocked.add(cmd_hash)

    async def pending_without_process():
        return (
            cmd_hash in terminal.finalization_pending
            and cmd_hash not in terminal.processes
        )

    await wait_for(pending_without_process)
    health = await terminal.health()
    assert health["ok"] is False
    assert health["degraded"] is True
    assert cmd_hash in health["finalization_pending_commands"]
    assert cmd_hash not in health["running_commands"]
    assert terminal.workers[1].done() is False

    blocked.remove(cmd_hash)

    async def completed():
        current = await repo.get(cmd_hash)
        return current if current and current.status == "completed" else None

    current = await wait_for(completed)
    assert current.exit_code == 0
    health = await terminal.health()
    assert health["degraded"] is False
    assert cmd_hash not in health["finalization_pending_commands"]
    assert terminal.workers[1].done() is False
    await terminal.stop()


@pytest.mark.asyncio
async def test_reconciler_repairs_multiple_dead_process_rows_across_queues(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    first = await repo.create("printf a", status="running", queue_id=1)
    second = await repo.create("printf b", status="running", queue_id=2)
    await repo.set_pid(first.cmd_hash, 99_999_991)
    await repo.set_pid(second.cmd_hash, 99_999_992)
    await terminal.start()

    async def both_failed():
        a = await repo.get(first.cmd_hash)
        b = await repo.get(second.cmd_hash)
        return (
            a
            if a and b and a.status == "failed" and b.status == "failed"
            else None
        )

    await wait_for(both_failed)
    first_now = await repo.get(first.cmd_hash)
    second_now = await repo.get(second.cmd_hash)
    assert first_now.error == "runtime.reconcile: process no longer exists"
    assert second_now.error == "runtime.reconcile: process no longer exists"
    assert all(not worker.done() for worker in terminal.workers.values())
    await terminal.stop()


@pytest.mark.asyncio
async def test_health_reports_dead_durable_running_as_stale_not_live(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    command = await repo.create("printf stale", status="running", queue_id=3)
    await repo.set_pid(command.cmd_hash, 99_999_993)

    health = await terminal.health()
    assert command.cmd_hash not in health["running_commands"]
    assert command.cmd_hash in health["stale_running_commands"]
    assert health["queues"][2]["running"] is None
    assert health["queues"][2]["durable_running"] == command.cmd_hash
    assert health["degraded"] is True
    assert health["ok"] is False


@pytest.mark.asyncio
async def test_health_shows_actual_live_process_when_older_durable_row_is_stale(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    stale = await repo.create("printf stale", status="running", queue_id=1)
    live = await repo.create("sleep 5", status="running", queue_id=1)
    await repo.set_pid(stale.cmd_hash, 99_999_994)
    await repo.set_pid(live.cmd_hash, 12_345)

    class FakeProcess:
        returncode = None

    terminal.processes[live.cmd_hash] = FakeProcess()
    terminal.process_queues[live.cmd_hash] = 1

    health = await terminal.health()
    assert health["queues"][0]["durable_running"] == stale.cmd_hash
    assert health["queues"][0]["running"] == live.cmd_hash
    assert health["running_commands"] == [live.cmd_hash]
    assert stale.cmd_hash in health["stale_running_commands"]

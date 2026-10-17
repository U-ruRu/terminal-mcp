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
async def test_terminalization_retries_without_killing_worker(tmp_path, monkeypatch):
    failures = 1
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


async def test_reconciler_finalizes_dead_process_rows_across_queues(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    first = await repo.create("printf a", status="running", queue_id=1)
    second = await repo.create("printf b", status="running", queue_id=2)
    await repo.set_pid(first.cmd_hash, 99_999_991)
    await repo.set_pid(second.cmd_hash, 99_999_992)

    await terminal._reconcile_processless_running()

    for command in (first, second):
        current = await repo.get(command.cmd_hash)
        assert current.status == "failed"
        assert current.error == "runtime.reconcile: process no longer exists"


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
async def test_start_reconciles_inherited_pidless_running_before_workers(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    command = await repo.create("printf inherited", status="running", queue_id=1)

    await terminal.start()

    current = await repo.get(command.cmd_hash)
    assert current.status == "failed"
    assert current.pid is None
    assert current.error == "runtime.reconcile: process was never attached"
    await terminal.stop()

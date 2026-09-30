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


@pytest.mark.asyncio
async def test_start_reconciles_inherited_pidless_running_before_workers(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    command = await repo.create("printf inherited", status="running", queue_id=1)

    await terminal.start()

    current = await repo.get(command.cmd_hash)
    assert current.status == "failed"
    assert current.pid is None
    assert current.error == "runtime.reconcile: process was never attached"
    health = await terminal.health()
    assert command.cmd_hash not in health["running_commands"]
    assert command.cmd_hash not in health["stale_running_commands"]
    assert health["degraded"] is False
    assert health["ok"] is True
    await terminal.stop()


@pytest.mark.asyncio
async def test_health_reports_unowned_pidless_running_as_stale(tmp_path):
    repo, terminal, _service = await create_runtime(tmp_path)
    command = await repo.create("printf orphan", status="running", queue_id=2)

    health = await terminal.health()

    assert command.cmd_hash not in health["running_commands"]
    assert command.cmd_hash in health["stale_running_commands"]
    assert health["queues"][1]["running"] is None
    assert health["queues"][1]["durable_running"] == command.cmd_hash
    assert health["degraded"] is True
    assert health["ok"] is False


@pytest.mark.asyncio
async def test_claim_commit_window_stays_truthful_until_local_ownership_is_known(
    tmp_path, monkeypatch
):
    repo, terminal, _service = await create_runtime(tmp_path)
    await terminal.start()

    original_claim = repo.claim_next
    original_execute = terminal._execute
    claim_committed = asyncio.Event()
    release_claim = asyncio.Event()
    execute_entered = asyncio.Event()
    release_execute = asyncio.Event()

    async def delayed_claim(queue_id):
        command = await original_claim(queue_id)
        if command is not None:
            claim_committed.set()
            await release_claim.wait()
        return command

    async def delayed_execute(command, **kwargs):
        execute_entered.set()
        await release_execute.wait()
        return await original_execute(command, **kwargs)

    monkeypatch.setattr(repo, "claim_next", delayed_claim)
    monkeypatch.setattr(terminal, "_execute", delayed_execute)

    command = await repo.create("true", queue_id=1)
    terminal.queue_events[1].set()
    await asyncio.wait_for(claim_committed.wait(), 1.0)

    await terminal._reconcile_processless_running()
    uncertain = await terminal.health()
    current = await repo.get(command.cmd_hash)
    assert current.status == "running"
    assert current.pid is None
    assert command.cmd_hash in uncertain["stale_running_commands"]
    assert uncertain["degraded"] is True

    release_claim.set()
    await asyncio.wait_for(execute_entered.wait(), 1.0)
    await terminal._reconcile_processless_running()
    owned = await terminal.health()
    current = await repo.get(command.cmd_hash)
    assert current.status == "running"
    assert current.pid is None
    assert command.cmd_hash not in owned["stale_running_commands"]
    assert owned["degraded"] is False
    assert owned["ok"] is True

    release_execute.set()

    async def completed():
        current = await repo.get(command.cmd_hash)
        return current if current and current.status == "completed" else None

    await wait_for(completed)
    await terminal.stop()


def test_sqlite_finalization_retry_uses_slower_ioerr_backoff():
    class IoErr(Exception):
        sqlite_errorname = "SQLITE_IOERR_LOCK"

    class Busy(Exception):
        sqlite_errorname = "SQLITE_BUSY"

    assert LinuxTerminalAdapter._storage_retry_delay(IoErr(), 1) == 0.25
    assert LinuxTerminalAdapter._storage_retry_delay(IoErr(), 3) == 1.0
    assert LinuxTerminalAdapter._storage_retry_delay(Busy(), 1) == 0.1
    assert LinuxTerminalAdapter._storage_retry_delay(Exception(), 1) == 0.05

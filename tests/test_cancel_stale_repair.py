import asyncio
import time

import pytest

import terminal_mcp.terminal.linux as linux_module
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
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


@pytest.mark.asyncio
async def test_cancel_repairs_dead_process_running_without_wait_or_kill(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    command = await repo.create("printf stale", status="running", queue_id=1)
    await repo.set_pid(command.cmd_hash, 99_999_901)

    def forbidden_kill(*_args, **_kwargs):
        raise AssertionError("stale repair must not signal a non-owned process")

    monkeypatch.setattr(linux_module.os, "killpg", forbidden_kill)

    started = time.monotonic()
    result = await service.cancel(command.cmd_hash)
    elapsed = time.monotonic() - started
    current = await repo.get(command.cmd_hash)

    assert elapsed < 0.5
    assert result["ok"] is True
    assert result["cancelled_from"] == "running"
    assert result["execution_started"] is True
    assert current.status == "cancelled"
    assert current.pid == 99_999_901
    health = await terminal.health()
    assert command.cmd_hash not in health["running_commands"]
    assert command.cmd_hash not in health["stale_running_commands"]
    assert health["degraded"] is False


@pytest.mark.asyncio
async def test_cancel_repairs_only_selected_stale_row_across_queues(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    first = await repo.create("printf first", status="running", queue_id=1)
    second = await repo.create("printf second", status="running", queue_id=3)
    await repo.set_pid(first.cmd_hash, 99_999_902)
    await repo.set_pid(second.cmd_hash, 99_999_903)

    first_result = await service.cancel(first.cmd_hash)
    health = await terminal.health()

    assert first_result["ok"] is True
    assert (await repo.get(first.cmd_hash)).status == "cancelled"
    assert (await repo.get(second.cmd_hash)).status == "running"
    assert first.cmd_hash not in health["stale_running_commands"]
    assert second.cmd_hash in health["stale_running_commands"]

    second_result = await service.cancel(second.cmd_hash)
    assert second_result["ok"] is True
    assert (await repo.get(second.cmd_hash)).status == "cancelled"
    assert (await terminal.health())["stale_running_commands"] == []


@pytest.mark.asyncio
async def test_repeated_cancel_of_repaired_running_row_is_idempotent(tmp_path):
    repo, _terminal, service = await runtime(tmp_path)
    command = await repo.create("printf stale", status="running", queue_id=2)
    await repo.set_pid(command.cmd_hash, 99_999_904)

    first = await service.cancel(command.cmd_hash)
    second = await service.cancel(command.cmd_hash)

    assert first["ok"] is True
    assert second == {
        "ok": True,
        "cmd_hash": command.cmd_hash,
        "error": None,
        "cancelled_from": "running",
        "execution_started": True,
    }
    assert (await repo.get(command.cmd_hash)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_repairs_pre_spawn_claim_without_waiting_for_process(tmp_path):
    repo, _terminal, service = await runtime(tmp_path)
    command = await repo.create("printf claimed", status="running", queue_id=4)

    started = time.monotonic()
    result = await service.cancel(command.cmd_hash)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result["ok"] is True
    assert result["execution_started"] is True
    assert (await repo.get(command.cmd_hash)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_overrides_pending_late_finalization_and_clears_pending(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    command = await repo.create("printf done", status="running", queue_id=1)
    await repo.set_pid(command.cmd_hash, 99_999_905)
    terminal.finalization_pending[command.cmd_hash] = {
        "status": "completed",
        "exit_code": 0,
        "error": None,
        "queue_id": 1,
        "attempt": 3,
        "exception_class": "OperationalError",
    }

    result = await service.cancel(command.cmd_hash)
    current = await repo.get(command.cmd_hash)

    assert result["ok"] is True
    assert current.status == "cancelled"
    assert current.exit_code == 0
    assert command.cmd_hash not in terminal.finalization_pending

    await terminal._reconcile_processless_running()
    assert (await repo.get(command.cmd_hash)).status == "cancelled"


@pytest.mark.asyncio
async def test_stale_cancel_and_late_finalization_converge_atomically(tmp_path):
    repo, _terminal, _service = await runtime(tmp_path)

    for index in range(20):
        command = await repo.create(
            f"printf race-{index}",
            status="running",
            queue_id=1,
        )
        pid = 99_998_000 + index
        await repo.set_pid(command.cmd_hash, pid)

        (observed, repaired), finalized = await asyncio.gather(
            repo.cancel_stale_running(command.cmd_hash, pid, 0),
            repo.finish_running(command.cmd_hash, "completed", 0, None),
        )
        current = await repo.get(command.cmd_hash)

        assert current.status in {"cancelled", "completed"}
        assert current.status != "running"
        if repaired:
            assert observed.status == "cancelled"
            assert finalized is False
        else:
            assert observed.status in {"cancelled", "completed"}


@pytest.mark.asyncio
async def test_cancel_racing_reconciler_never_leaves_running(tmp_path):
    repo, terminal, service = await runtime(tmp_path)

    for index in range(10):
        command = await repo.create(
            f"printf reconcile-{index}",
            status="running",
            queue_id=(index % 4) + 1,
        )
        await repo.set_pid(command.cmd_hash, 99_997_000 + index)

        result, _ = await asyncio.gather(
            service.cancel(command.cmd_hash),
            terminal._reconcile_processless_running(),
        )
        current = await repo.get(command.cmd_hash)

        assert current.status in {"cancelled", "failed"}
        assert current.status != "running"
        if current.status == "cancelled":
            assert result["ok"] is True
        else:
            assert result["ok"] is False


@pytest.mark.asyncio
async def test_cancel_does_not_touch_existing_unowned_process(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    command = await repo.create("sleep 30", status="running", queue_id=1)
    await repo.set_pid(command.cmd_hash, 12_345)

    monkeypatch.setattr(terminal, "_pid_exists", lambda pid: pid == 12_345)

    def forbidden_kill(*_args, **_kwargs):
        raise AssertionError("unowned process must not be signalled")

    monkeypatch.setattr(linux_module.os, "killpg", forbidden_kill)

    result = await service.cancel(command.cmd_hash)
    current = await repo.get(command.cmd_hash)

    assert result["ok"] is False
    assert result["error"] == (
        "cancel.unowned_process: durable process exists without local ownership"
    )
    assert current.status == "running"

import pytest

import terminal_mcp.terminal.in_process as linux_module
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

    result = await service.cancel(command.cmd_hash)
    current = await repo.get(command.cmd_hash)

    assert result["ok"] is True
    assert result["cancelled_from"] == "running"
    assert result["execution_started"] is True
    assert current.status == "cancelled"
    assert current.pid == 99_999_901


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


@pytest.mark.asyncio
async def test_cancel_does_not_touch_existing_unowned_process(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    command = await repo.create("sleep 30", status="running", queue_id=1)
    await repo.set_pid(command.cmd_hash, 12_345)

    async def pid_exists(pid):
        return pid == 12_345

    monkeypatch.setattr(terminal, "_pid_exists", pid_exists)

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

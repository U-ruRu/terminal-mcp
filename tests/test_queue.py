import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def create_runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def test_queue_claim_selects_oldest_waiting_command(tmp_path):
    repo = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    first = await repo.create("printf first", status="queued", queue_id=1)
    await repo.create("printf second", status="queued", queue_id=1)

    claimed = await repo.claim_next(1)

    assert claimed.cmd_hash == first.cmd_hash
    assert claimed.status == "running"


@pytest.mark.asyncio
async def test_recovery_nonzero_exit_is_command_failure_not_plugin_error(tmp_path):
    _, terminal, service = await create_runtime(tmp_path)
    result = await service.recovery("printf 'command-error\\n' >&2; exit 7")
    assert result["ok"] is True
    assert result["exit_code"] == 7
    assert result["error"] is None
    assert result["lines"][0].endswith("command-error")

    await terminal.stop()

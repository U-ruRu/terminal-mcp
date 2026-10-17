import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


@pytest.mark.asyncio
async def test_cancel_before_claim_linearizes_to_pre_start_cancel(tmp_path):
    repo, _terminal, _service = await runtime(tmp_path)
    command = await repo.create("printf 'must-not-run\n'", status="queued", queue_id=1)

    observed, cancelled_before_start = await repo.cancel_if_queued(command.cmd_hash)
    assert cancelled_before_start is True
    assert observed is not None
    assert observed.status == "cancelled"
    assert observed.claimed_at is None
    assert observed.started_at is None
    assert observed.finished_at is not None


@pytest.mark.asyncio
async def test_claim_before_cancel_linearizes_to_running_cancel_path(tmp_path):
    repo, _terminal, _service = await runtime(tmp_path)
    command = await repo.create("printf 'may-have-started\n'", status="queued", queue_id=1)
    await repo.claim_next(1)

    observed, cancelled_before_start = await repo.cancel_if_queued(command.cmd_hash)

    assert cancelled_before_start is False
    assert observed is not None
    assert observed.status == "running"
    assert observed.claimed_at is not None
    assert observed.started_at is not None

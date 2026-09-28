import asyncio

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
    claimed = await repo.claim_next(1)

    assert cancelled_before_start is True
    assert observed is not None
    assert observed.status == "cancelled"
    assert observed.claimed_at is None
    assert observed.started_at is None
    assert observed.finished_at is not None
    assert claimed is None


@pytest.mark.asyncio
async def test_claim_before_cancel_linearizes_to_running_cancel_path(tmp_path):
    repo, _terminal, _service = await runtime(tmp_path)
    command = await repo.create("printf 'may-have-started\n'", status="queued", queue_id=1)

    claimed = await repo.claim_next(1)
    observed, cancelled_before_start = await repo.cancel_if_queued(command.cmd_hash)

    assert claimed is not None
    assert claimed.status == "running"
    assert cancelled_before_start is False
    assert observed is not None
    assert observed.status == "running"
    assert observed.claimed_at is not None
    assert observed.started_at is not None


@pytest.mark.asyncio
async def test_concurrent_claim_cancel_has_exactly_one_queued_winner(tmp_path):
    repo, _terminal, _service = await runtime(tmp_path)

    for index in range(40):
        command = await repo.create(
            f"printf 'race-{index}\n'",
            status="queued",
            queue_id=1,
        )
        (observed, cancelled_before_start), claimed = await asyncio.gather(
            repo.cancel_if_queued(command.cmd_hash),
            repo.claim_next(1),
        )

        assert observed is not None
        if cancelled_before_start:
            assert observed.status == "cancelled"
            assert observed.claimed_at is None
            assert observed.started_at is None
            assert claimed is None
        else:
            assert claimed is not None
            assert claimed.cmd_hash == command.cmd_hash
            assert claimed.status == "running"
            assert observed.status == "running"
            assert observed.claimed_at is not None
            assert observed.started_at is not None
            assert await repo.finish_running(command.cmd_hash, "cancelled") is True


@pytest.mark.asyncio
async def test_service_cancel_reports_pre_start_provenance_and_read_keeps_it(tmp_path):
    repo, _terminal, service = await runtime(tmp_path)
    command = await repo.create("printf 'never-run\n'", status="queued", queue_id=1)

    result = await service.cancel(command.cmd_hash)
    read = await service.read(command.cmd_hash)

    assert result == {
        "ok": True,
        "cmd_hash": command.cmd_hash,
        "error": None,
        "cancelled_from": "queued",
        "execution_started": False,
    }
    assert read["status"] == "cancelled"
    assert read["execution_started"] is False
    assert read["claimed_at"] is None
    assert read["started_at"] is None
    assert read["finished_at"] is not None
    assert read["lines"] == []


@pytest.mark.asyncio
async def test_service_cancel_reports_running_provenance_and_read_keeps_it(tmp_path, monkeypatch):
    repo, terminal, service = await runtime(tmp_path)
    command = await repo.create("printf 'claimed\n'", status="running", queue_id=1)

    async def cancel_running(current, timeout_seconds=10):
        assert current.cmd_hash == command.cmd_hash
        assert timeout_seconds > 0
        assert await repo.finish_running(current.cmd_hash, "cancelled") is True
        return True, None

    monkeypatch.setattr(terminal, "cancel", cancel_running)

    result = await service.cancel(command.cmd_hash)
    read = await service.read(command.cmd_hash)

    assert result == {
        "ok": True,
        "cmd_hash": command.cmd_hash,
        "error": None,
        "cancelled_from": "running",
        "execution_started": True,
    }
    assert read["status"] == "cancelled"
    assert read["execution_started"] is True
    assert read["claimed_at"] is not None
    assert read["started_at"] is not None
    assert read["finished_at"] is not None


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_relabel_pre_start_cancel_as_running(tmp_path):
    repo, _terminal, service = await runtime(tmp_path)
    command = await repo.create("printf 'never-run\n'", status="queued", queue_id=1)

    first = await service.cancel(command.cmd_hash)
    second = await service.cancel(command.cmd_hash)

    assert first["cancelled_from"] == "queued"
    assert first["execution_started"] is False
    assert second["ok"] is False
    assert second["cancelled_from"] is None
    assert second["execution_started"] is False
    assert second["error"] == "cancel.state: command is already cancelled"

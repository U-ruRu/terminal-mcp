"""Exercise durable cancellation with real workers, processes and reopened storage."""

import asyncio
import shlex
from unittest.mock import Mock

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def _wait_for_command(repo, command_hash, predicate):
    async with asyncio.timeout(10):
        while True:
            command = await repo.get(command_hash)
            if command is not None and predicate(command):
                return command
            await asyncio.sleep(0.02)


def _scheduler(repo, tmp_path):
    return LinuxTerminalAdapter(
        repo,
        "/bin/bash",
        tmp_path,
        0.05,
        queue_reconcile_sec=0.02,
    )


@pytest.mark.asyncio
async def test_durable_cancel_stops_real_process_after_lost_wakeup(tmp_path, monkeypatch):
    repo = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    scheduler = _scheduler(repo, tmp_path)
    started = tmp_path / "started"
    escaped = tmp_path / "escaped"
    following = tmp_path / "following"
    await scheduler.start()
    try:
        command = await repo.create(
            f"printf started > {shlex.quote(str(started))}; sleep 30; "
            f"printf escaped > {shlex.quote(str(escaped))}",
            status="queued",
            queue_id=1,
        )
        await scheduler.submit(command)
        running = await _wait_for_command(
            repo,
            command.cmd_hash,
            lambda item: item.status == "running" and item.pid is not None and started.exists(),
        )
        assert await scheduler.process_exists(running.pid)

        with monkeypatch.context() as patch:
            patch.setattr(
                scheduler, "enqueue_cancel", Mock(side_effect=RuntimeError("lost wakeup"))
            )
            accepted, outcome = await repo.accept_cancel_intent(command.cmd_hash)
            assert outcome == "running"
            with pytest.raises(RuntimeError, match="lost wakeup"):
                scheduler.enqueue_cancel(accepted)

        cancelled = await _wait_for_command(
            repo,
            command.cmd_hash,
            lambda item: item.status == "cancelled",
        )
        assert cancelled.started_at is not None
        assert await repo.active_cancel_intents() == []
        assert not escaped.exists()
        assert not await scheduler.process_exists(running.pid)

        next_command = await repo.create(
            f"printf resumed >> {shlex.quote(str(following))}",
            status="queued",
            queue_id=1,
        )
        await scheduler.submit(next_command)
        completed = await _wait_for_command(
            repo,
            next_command.cmd_hash,
            lambda item: item.status == "completed",
        )
        assert completed.exit_code == 0
        assert following.read_text() == "resumed"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_cancelled_queued_command_never_runs_after_repository_reopen(tmp_path):
    database = tmp_path / "commands.sqlite3"
    output = tmp_path / "output.sqlite3"
    repo = SqliteRepository(database, output)
    await repo.initialize()
    forbidden = tmp_path / "must-not-run"
    following = tmp_path / "following"
    command = await repo.create(
        f"printf forbidden > {shlex.quote(str(forbidden))}",
        status="queued",
        queue_id=1,
    )
    accepted, outcome = await repo.accept_cancel_intent(command.cmd_hash)
    assert outcome == "queued" and accepted.cmd_hash == command.cmd_hash
    assert (await repo.get(command.cmd_hash)).status == "cancelled"

    reopened = SqliteRepository(database, output)
    await reopened.initialize()
    repeated, outcome = await reopened.accept_cancel_intent(command.cmd_hash)
    assert outcome == "previously_accepted" and repeated.status == "cancelled"
    scheduler = _scheduler(reopened, tmp_path)
    await scheduler.start()
    try:
        next_command = await reopened.create(
            f"printf resumed > {shlex.quote(str(following))}",
            status="queued",
            queue_id=1,
        )
        await scheduler.submit(next_command)
        await _wait_for_command(
            reopened,
            next_command.cmd_hash,
            lambda item: item.status == "completed",
        )
        stored = await reopened.get(command.cmd_hash)
        assert stored.status == "cancelled" and stored.started_at is None
        assert not forbidden.exists()
        assert following.read_text() == "resumed"
        assert await reopened.active_cancel_intents() == []
    finally:
        await scheduler.stop()

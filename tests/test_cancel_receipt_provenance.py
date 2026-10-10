"""Cancellation retries preserve whether execution actually started."""

import asyncio
import sqlite3

import pytest
from test_command_committed_receipts import IDENTITY, _attributed_command, _backend

from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("reopen", [False, True])
async def test_cancel_retries_preserve_original_execution_provenance(
    tmp_path, monkeypatch, status, reopen
):
    backend = await _backend(tmp_path, monkeypatch)
    command = await _attributed_command(backend, status=status)
    first = await backend.cancel(command.cmd_hash, **IDENTITY)
    assert first["ok"] is True
    assert first["cancelled_from"] == status
    assert first["execution_started"] is (status == "running")

    if reopen:
        backend.repo = SqliteRepository(backend.repo.path, tmp_path / "output.sqlite3")
    retried = await asyncio.gather(*(
        backend.cancel(command.cmd_hash, **IDENTITY) for _ in range(4)
    ))
    for result in retried:
        assert result["ok"] is True
        assert result["cmd_hash"] == command.cmd_hash
        assert result["cancelled_from"] == first["cancelled_from"]
        assert result["execution_started"] == first["execution_started"]
    with sqlite3.connect(backend.repo.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM command_cancel_intents WHERE cmd_hash=?",
            (command.cmd_hash,),
        ).fetchone()[0]
    assert count == 1
    if status == "queued":
        stored = await backend.repo.get(command.cmd_hash)
        assert stored.status == "cancelled"
        assert stored.claimed_at is None and stored.started_at is None
        backend.terminal.enqueue_cancel.assert_not_called()

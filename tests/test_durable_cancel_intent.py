"""Cancel-intent durability across duplicate calls and repository restarts."""

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_cancel_intent_is_committed_before_process_shutdown(tmp_path):
    path = tmp_path / "commands.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    queued = await repo.create("printf queued", cmd_hash="qtest-1", status="queued")
    first, outcome = await repo.accept_cancel_intent(queued.cmd_hash)
    assert first.cmd_hash == queued.cmd_hash and outcome == "queued"
    assert (await repo.get(queued.cmd_hash)).status == "cancelled"
    same, outcome = await repo.accept_cancel_intent(queued.cmd_hash)
    assert same.cmd_hash == queued.cmd_hash and outcome == "previously_accepted"

    running = await repo.create("printf running", cmd_hash="rtest-1", status="running")
    accepted, outcome = await repo.accept_cancel_intent(running.cmd_hash)
    assert accepted.cmd_hash == running.cmd_hash and outcome == "running"
    assert (await repo.get(running.cmd_hash)).status == "running"

    pending = await repo.active_cancel_intents()
    reopened = SqliteRepository(path, tmp_path / "output.sqlite3")
    assert [command.cmd_hash for command in pending] == ["rtest-1"]
    retry, outcome = await reopened.accept_cancel_intent(running.cmd_hash)
    assert retry.cmd_hash == running.cmd_hash and outcome == "previously_accepted"

    untouched = await repo.create("printf finished", cmd_hash="ctest-1", status="completed")
    rejected, outcome = await reopened.accept_cancel_intent(untouched.cmd_hash)
    assert rejected.cmd_hash == untouched.cmd_hash and outcome == "command_already_finished"
    missing, outcome = await reopened.accept_cancel_intent("unknown-command")
    assert missing is None and outcome == "command_not_found"

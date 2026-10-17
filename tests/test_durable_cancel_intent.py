"""Cancel-intent durability across duplicate calls and repository restarts."""

from terminal_mcp.storage.sqlite import SqliteRepository


async def test_queued_cancel_intent_persists_terminal_state(tmp_path):
    path = tmp_path / "commands.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    command = await repo.create("printf queued", status="queued")

    accepted, outcome = await repo.accept_cancel_intent(command.cmd_hash)

    assert outcome == "queued"
    assert accepted.cmd_hash == command.cmd_hash
    reopened = SqliteRepository(path, tmp_path / "output.sqlite3")
    assert (await reopened.get(command.cmd_hash)).status == "cancelled"


async def test_running_cancel_intent_persists_pending_shutdown(tmp_path):
    path = tmp_path / "commands.sqlite3"
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repo.initialize()
    command = await repo.create("printf running", status="running")

    accepted, outcome = await repo.accept_cancel_intent(command.cmd_hash)

    assert outcome == "running"
    assert accepted.status == "running"
    reopened = SqliteRepository(path, tmp_path / "output.sqlite3")
    assert [item.cmd_hash for item in await reopened.active_cancel_intents()] == [command.cmd_hash]

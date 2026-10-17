import sqlite3

import pytest

from terminal_mcp.storage.output import OutputStore


@pytest.mark.asyncio
async def test_physical_compaction_never_prunes_active_command(tmp_path):
    path = tmp_path / "output.sqlite3"
    store = OutputStore(
        path,
        line_max_bytes=256 * 1024,
        command_max_bytes=512 * 1024,
        target_bytes=128 * 1024,
        max_bytes=256 * 1024,
    )
    await store.initialize()
    await store.append_lines("active", ["x" * 220000])
    await store.append_lines("old", ["y" * 220000])

    pruned = await store.prune({"active"})
    assert pruned == ["old"]
    assert await store.command_meta("active") is not None
    assert await store.command_meta("old") is None


@pytest.mark.asyncio
async def test_pruned_output_scrubs_terminal_command_body_and_preserves_preview(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository

    repo = SqliteRepository(
        tmp_path / "runtime.sqlite3",
        tmp_path / "output.sqlite3",
        output_target_bytes=512,
        output_max_bytes=1024,
    )
    await repo.initialize()
    command = await repo.create(
        "printf 'full secret-bearing command body'",
        status="completed",
        agent_id="agent-1",
        command_preview="printf 'full secret…'",
    )
    await repo.append_lines(command.cmd_hash, ["x" * 2048])

    assert await repo.prune_output_cache() == [command.cmd_hash]
    retained = await repo.get(command.cmd_hash)
    assert retained is not None
    assert retained.cmd == "[command body pruned]"
    assert retained.status == "completed"
    status = await repo.output_status(command.cmd_hash)
    assert status["output_retained"] is False

    with sqlite3.connect(repo.path) as db:
        preview = db.execute(
            "SELECT command_preview FROM command_agent_attribution WHERE command_hash=?",
            (command.cmd_hash,),
        ).fetchone()
    assert preview == ("printf 'full secret…'",)


async def test_output_line_limit_keeps_valid_utf8(tmp_path):
    store = OutputStore(tmp_path / "output.sqlite3", line_max_bytes=90, command_max_bytes=180)
    await store.initialize()

    result = await store.append_lines("unicode", ["ёж" * 100])

    assert result["truncated"] is True
    lines = await store.read_command_lines("unicode", limit=2, offset=0)
    assert len(lines) == 1
    assert len(lines[0].text.encode("utf-8")) <= 90
    assert lines[0].text.startswith("ёж")
    assert "truncated" in lines[0].text


async def test_command_output_stops_at_configured_byte_budget(tmp_path):
    store = OutputStore(tmp_path / "output.sqlite3", line_max_bytes=64, command_max_bytes=96)
    await store.initialize()

    result = await store.append_lines("bounded", ["x" * 64, "y" * 64, "z" * 64])

    assert result == {"accepting": False, "truncated": True, "stored_bytes": 96, "stored_lines": 2}
    lines = await store.read_command_lines("bounded", limit=3, offset=0)
    assert [line.text for line in lines] == ["x" * 64, "y" * 32]

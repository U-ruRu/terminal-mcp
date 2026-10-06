import sqlite3

import pytest

from terminal_mcp.storage.output import OutputStore


@pytest.mark.asyncio
async def test_physical_pressure_compacts_legacy_cache_without_logical_prune(tmp_path):
    path = tmp_path / "output.sqlite3"
    # Simulate a pre-auto-vacuum cache with real allocated pages and no retained rows.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA auto_vacuum=NONE")
        db.execute("CREATE TABLE legacy_padding(value BLOB)")
        db.execute("INSERT INTO legacy_padding(value) VALUES(zeroblob(768000))")
        db.commit()
        db.execute("DROP TABLE legacy_padding")
        db.commit()
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0

    store = OutputStore(path, target_bytes=128 * 1024, max_bytes=256 * 1024)
    await store.initialize()
    before = await store.stats()
    assert before["used_bytes"] == 0
    assert before["allocated_bytes"] > store.max_bytes

    assert await store.prune() == []
    after = await store.stats()
    assert after["allocated_bytes"] <= store.max_bytes
    assert after["allocated_bytes"] < before["allocated_bytes"]
    assert after["last_prune_at"] is not None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


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


@pytest.mark.asyncio
async def test_prune_never_scrubs_queued_or_running_command_bodies(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository

    repo = SqliteRepository(
        tmp_path / "runtime.sqlite3",
        tmp_path / "output.sqlite3",
        output_target_bytes=512,
        output_max_bytes=1024,
    )
    await repo.initialize()
    queued = await repo.create("echo queued-secret", status="queued", queue_id=1)
    running = await repo.create("echo running-secret", status="running", queue_id=2)
    await repo.append_lines(queued.cmd_hash, ["q" * 2048])
    await repo.append_lines(running.cmd_hash, ["r" * 2048])

    assert await repo.prune_output_cache() == []
    assert (await repo.get(queued.cmd_hash)).cmd == "echo queued-secret"
    assert (await repo.get(running.cmd_hash)).cmd == "echo running-secret"


@pytest.mark.asyncio
async def test_initialize_scrubs_previously_pruned_terminal_command_body(tmp_path):
    from terminal_mcp.storage.sqlite import SqliteRepository

    runtime = tmp_path / "runtime.sqlite3"
    output = tmp_path / "output.sqlite3"
    repo = SqliteRepository(runtime, output)
    await repo.initialize()
    command = await repo.create("echo historical-secret", status="failed")
    with sqlite3.connect(runtime) as db:
        db.execute(
            "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,0,?)",
            (command.cmd_hash, "2026-10-06T00:00:00Z"),
        )
        db.commit()

    restarted = SqliteRepository(runtime, output)
    await restarted.initialize()
    retained = await restarted.get(command.cmd_hash)
    assert retained is not None
    assert retained.cmd == "[command body pruned]"

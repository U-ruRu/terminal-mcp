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

"""SQLite repository integrity, cache, cancellation and bounded worker cleanup."""

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository


def _corrupt_instance_events_index(path: Path) -> None:
    # Only test-owned, initialized databases. Never run against live storage.
    with sqlite3.connect(path) as db:
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint[0] == 0
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='ix_instance_events_type_seq'"
        ).fetchone()[0]
    assert root > 1
    with path.open("r+b") as dbfile:
        dbfile.seek((root - 1) * page_size)
        assert dbfile.read(1) in (b"\x02", b"\x05", b"\x0a", b"\x0d")
        dbfile.seek((root - 1) * page_size)
        dbfile.write(b"\x00")
        dbfile.flush()
        os.fsync(dbfile.fileno())


async def test_main_db_health_detects_corrupted_event_index(tmp_path):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    _corrupt_instance_events_index(path)

    assert await repo.ping() is False


@pytest.mark.asyncio
async def test_main_db_health_detects_removed_file_even_with_cached_success(tmp_path):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    await repo.ping()
    path.unlink()
    assert await repo.ping() is False
    assert not path.exists()


async def test_main_db_health_reports_probe_timeout(tmp_path, monkeypatch):
    import terminal_mcp.storage.sqlite as sqlite_module

    repo = SqliteRepository(tmp_path / "main.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()

    async def stalled_probe(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(repo, "_health_integrity_probe", stalled_probe)
    monkeypatch.setattr(sqlite_module, "HEALTH_INTEGRITY_TIMEOUT_SECONDS", 0.01)

    assert await repo.ping() is False


# Real SQLite VM work exercises SQL interrupt and queued worker cleanup.
_NATIVE_SLOW_QUERY = (
    "WITH RECURSIVE spin(n) AS (VALUES(0) "
    "UNION ALL SELECT n + 1 FROM spin WHERE n < 4000000) "
    "SELECT max(n) FROM spin"
)


async def test_main_db_health_rechecks_expired_cache(tmp_path, monkeypatch):
    import time
    from unittest.mock import AsyncMock

    import terminal_mcp.storage.sqlite as sqlite_module

    repo = SqliteRepository(tmp_path / "main.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    await repo.ping()
    repo._health_checked_at = time.monotonic() - sqlite_module.HEALTH_INTEGRITY_CACHE_SECONDS
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(repo, "_health_integrity_probe", probe)

    assert await repo.ping() is False
    probe.assert_awaited_once()

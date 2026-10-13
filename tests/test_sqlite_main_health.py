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


@pytest.mark.asyncio
async def test_main_db_health_does_not_create_missing_database(tmp_path):
    path = tmp_path / "absent.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    assert await repo.ping() is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_main_db_health_fails_when_instance_events_index_corrupted(tmp_path):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    assert await repo.ping() is True
    _corrupt_instance_events_index(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("SELECT 1").fetchone() == (1,)
        try:
            integrity = db.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError:
            integrity = None
        assert integrity != ("ok",)
    # The integrity check is cached for 30s, but a new runtime must detect
    # corruption immediately; the existing runtime must detect it at TTL.
    restarted = SqliteRepository(path, tmp_path / "out.sqlite3")
    assert await restarted.ping() is False
    repo._health_checked_at = 0.0
    assert await repo.ping() is False


@pytest.mark.asyncio
async def test_main_db_health_bounded_probe_singleflight(tmp_path, monkeypatch):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    original = repo._health_integrity_probe
    calls = 0

    async def counted(**kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await original(**kwargs)

    monkeypatch.setattr(repo, "_health_integrity_probe", counted)
    assert await asyncio.gather(*(repo.ping() for _ in range(12))) == [True] * 12
    assert calls == 1


@pytest.mark.asyncio
async def test_main_db_health_detects_removed_file_even_with_cached_success(tmp_path):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    assert await repo.ping() is True
    path.unlink()
    assert await repo.ping() is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_main_db_health_times_out_closed_and_caches_failure(tmp_path, monkeypatch):
    import terminal_mcp.storage.sqlite as sqlite_module

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    calls = 0

    async def hangs(**_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return True

    monkeypatch.setattr(repo, "_health_integrity_probe", hangs)
    monkeypatch.setattr(sqlite_module, "HEALTH_INTEGRITY_TIMEOUT_SECONDS", 0.01)
    assert await repo.ping() is False
    assert await repo.ping() is False
    assert calls == 1


@pytest.mark.asyncio
async def test_main_db_health_cancellation_does_not_poison_next_probe(tmp_path, monkeypatch):
    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    started = asyncio.Event()
    gate = asyncio.Event()
    original = repo._health_integrity_probe

    async def waiting_probe(**kwargs):
        started.set()
        await gate.wait()
        return await original(**kwargs)

    monkeypatch.setattr(repo, "_health_integrity_probe", waiting_probe)
    task = asyncio.create_task(repo.ping())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(repo, "_health_integrity_probe", original)
    assert await repo.ping() is True


# Real SQLite VM work exercises SQL interrupt and queued worker cleanup.
_NATIVE_SLOW_QUERY = (
    "WITH RECURSIVE spin(n) AS (VALUES(0) "
    "UNION ALL SELECT n + 1 FROM spin WHERE n < 4000000) "
    "SELECT max(n) FROM spin"
)


@pytest.mark.asyncio
async def test_main_db_native_sql_timeout_covers_worker_shutdown(tmp_path, monkeypatch):
    import time

    import terminal_mcp.storage.sqlite as sqlite_module

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    connections = []
    connect = sqlite_module.aiosqlite.connect

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    probe = repo._health_integrity_probe

    async def native_probe(*, deadline):
        return await probe(deadline=deadline, statement=_NATIVE_SLOW_QUERY)

    monkeypatch.setattr(sqlite_module.aiosqlite, "connect", tracked_connect)
    monkeypatch.setattr(sqlite_module, "HEALTH_INTEGRITY_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(repo, "_health_integrity_probe", native_probe)
    start = time.monotonic()
    assert await repo.ping() is False
    elapsed = time.monotonic() - start
    assert elapsed < 0.15, f"SQL/cleanup exceeded hard budget: {elapsed:.3f}s"
    assert len(connections) == 1
    assert not connections[0]._thread.is_alive(), "health worker remained alive after timeout"

    # The timeout result is cached; there should be no second SQLite worker.
    assert await repo.ping() is False
    assert len(connections) == 1


@pytest.mark.asyncio
async def test_main_db_cancel_interrupts_active_native_sql_and_recovers(tmp_path, monkeypatch):
    import threading
    import time

    import aiosqlite

    import terminal_mcp.storage.sqlite as sqlite_module

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    started = asyncio.Event()
    signalled = threading.Event()
    loop = asyncio.get_running_loop()
    connections = []
    connect = sqlite_module.aiosqlite.connect
    register_progress = aiosqlite.Connection.set_progress_handler
    probe = repo._health_integrity_probe

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    async def tracking_progress(self, callback, steps):
        def observed_callback():
            if not signalled.is_set():
                signalled.set()
                loop.call_soon_threadsafe(started.set)
            return callback()

        await register_progress(self, observed_callback, steps)

    async def native_probe(*, deadline):
        return await probe(deadline=deadline, statement=_NATIVE_SLOW_QUERY)

    monkeypatch.setattr(sqlite_module.aiosqlite, "connect", tracked_connect)
    monkeypatch.setattr(aiosqlite.Connection, "set_progress_handler", tracking_progress)
    monkeypatch.setattr(repo, "_health_integrity_probe", native_probe)
    task = asyncio.create_task(repo.ping())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    before_cancel = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.15)
    assert time.monotonic() - before_cancel < 0.15
    assert len(connections) == 1
    assert not connections[0]._thread.is_alive()

    monkeypatch.setattr(repo, "_health_integrity_probe", probe)
    assert await repo.ping() is True


@pytest.mark.asyncio
async def test_main_db_health_cache_ttl_both_outcomes(tmp_path, monkeypatch):
    import time

    import terminal_mcp.storage.sqlite as sqlite_module

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    assert await repo.ping() is True
    calls = 0
    failing = True

    async def counted_probe(**_kwargs):
        nonlocal calls
        calls += 1
        return not failing

    monkeypatch.setattr(repo, "_health_integrity_probe", counted_probe)
    assert await repo.ping() is True  # successful cache hit
    assert calls == 0

    repo._health_checked_at = time.monotonic() - sqlite_module.HEALTH_INTEGRITY_CACHE_SECONDS
    assert await repo.ping() is False
    assert calls == 1
    assert await repo.ping() is False  # failures cached too
    assert calls == 1

    failing = False
    repo._health_checked_at = time.monotonic() - sqlite_module.HEALTH_INTEGRITY_CACHE_SECONDS
    assert await repo.ping() is True  # recovery is re-evaluated at TTL
    assert calls == 2


@pytest.mark.asyncio
async def test_main_db_health_lock_wait_has_own_deadline(tmp_path, monkeypatch):
    import time

    import terminal_mcp.storage.sqlite as sqlite_module

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    monkeypatch.setattr(sqlite_module, "HEALTH_INTEGRITY_TIMEOUT_SECONDS", 0.02)
    await repo._health_probe_lock.acquire()
    try:
        start = time.monotonic()
        assert await repo.ping() is False
        assert time.monotonic() - start < 0.15
    finally:
        repo._health_probe_lock.release()
    # A lock wait timeout does not poison the later genuine integrity check.
    assert await repo.ping() is True

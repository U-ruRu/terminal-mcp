"""SQLite main database must not be opened as a raw file by our connection path.

POSIX fcntl locks belong to a process, not to an individual file descriptor.
Closing an unrelated raw fd drops locks held by active SQLite connections,
which can cause WAL/B-tree corruption during concurrent Mesh writes.
"""

from pathlib import Path

import aiosqlite
import pytest

from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.storage.application_uow import SqliteApplicationUnitOfWork
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection


@pytest.mark.asyncio
async def test_hot_connections_never_open_close_raw_sqlite_main_file(tmp_path, monkeypatch):
    path = tmp_path / "production.sqlite3"
    await SqliteRepository(path, tmp_path / "outputs.sqlite3").initialize()
    original_open = Path.open
    intercepted = []

    def reject_raw_main_open(self, mode="r", *args, **kwargs):
        if self == path and "r" in mode and "b" in mode:
            intercepted.append((str(self), mode))
            raise AssertionError("Raw SQLite file open/close breaks process POSIX locks")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_raw_main_open)

    # Mesh/Event/Task stores use cancellation_safe_connection for each access;
    # UOW used to repeat the same dangerous header read independently.
    for _ in range(20):
        async with cancellation_safe_connection(aiosqlite.connect, path) as db:
            result = await (await db.execute("PRAGMA schema_version")).fetchone()
            assert isinstance(result[0], int)
        async with SqliteApplicationUnitOfWork(path).transaction():
            pass

    assert intercepted == []


@pytest.mark.asyncio
async def test_fleet_control_health_and_network_writes_never_open_close_raw_db(
    tmp_path, monkeypatch
):
    path = tmp_path / "fleet-control.sqlite3"
    store = FleetControlStore(
        path, fleet_id="fleet-test", node_id="firstbyte", control_node_id="firstbyte"
    )
    await store.initialize()
    original_open = Path.open
    intercepted = []

    def reject_raw_control_open(self, mode="r", *args, **kwargs):
        if self == path and "r" in mode and "b" in mode:
            intercepted.append((str(self), mode))
            raise AssertionError("Fleet control raw open closes POSIX locks")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_raw_control_open)
    for _ in range(20):
        assert await store.healthy() is True
        assert await store.schema_version() == store.SCHEMA_VERSION
    assert intercepted == []

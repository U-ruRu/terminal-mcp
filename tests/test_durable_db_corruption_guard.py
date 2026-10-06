from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from terminal_mcp.storage.sqlite import DurableDatabaseError, SqliteRepository


async def initialized_repository(tmp_path: Path) -> SqliteRepository:
    repo = SqliteRepository(tmp_path / "durable.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    return repo


def overwrite_header(path: Path, payload: bytes) -> bytes:
    original = path.read_bytes()[:4096]
    with path.open("r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.write(b"\x00" * (4096 - len(payload)))
    return original


@pytest.mark.asyncio
async def test_http_error_overwrite_is_detected_without_mutating_forensic_bytes(tmp_path):
    repo = await initialized_repository(tmp_path)
    payload = (
        b"HTTP/1.1 500 Internal Server Error\r\n"
        b"server: uvicorn\r\n"
        b"content-length: 21\r\n\r\n"
        b"Internal Server Error"
    )
    overwrite_header(Path(repo.path), payload)
    corrupted = await asyncio.to_thread(Path(repo.path).read_bytes)

    assert await repo.ping() is False
    assert await asyncio.to_thread(Path(repo.path).read_bytes) == corrupted
    with pytest.raises(DurableDatabaseError, match="durable_database_invalid_header"):
        await repo.initialize()
    assert await asyncio.to_thread(Path(repo.path).read_bytes) == corrupted


@pytest.mark.asyncio
async def test_wal_header_overwrite_is_classified_explicitly(tmp_path):
    repo = await initialized_repository(tmp_path)
    overwrite_header(Path(repo.path), bytes.fromhex("377f0682") + b"\x00" * 28)

    assert await repo.ping() is False
    with pytest.raises(DurableDatabaseError, match="durable_database_main_is_wal"):
        await repo.initialize()


@pytest.mark.asyncio
async def test_empty_new_database_still_initializes(tmp_path):
    path = tmp_path / "durable.sqlite3"
    path.touch()
    repo = SqliteRepository(path, tmp_path / "output.sqlite3")

    await repo.initialize()

    assert await repo.ping() is True
    assert path.read_bytes().startswith(b"SQLite format 3\x00")

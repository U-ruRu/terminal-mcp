"""AgentStore owns the durable uniqueness of a full session identity."""

import sqlite3

import pytest

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


async def test_session_identity_is_unique(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3")
    await repo.initialize()
    store = AgentStore(repo.path)
    now = utc_text()
    await store.create_session("Alpha-1111", "one", "first", ["repo:storage"], ["first"], 1, now)
    with pytest.raises(sqlite3.IntegrityError):
        await store.create_session("Alpha-1111", "two", "second", ["repo:mcp"], ["second"], 1, now)

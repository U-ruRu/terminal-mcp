"""Public service health over real repository, workflow and event-journal storage."""

import sqlite3

import pytest

from terminal_mcp.storage.sqlite import SqliteRepository


async def test_repository_failure_maps_to_public_storage_health(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    repo = SqliteRepository(tmp_path / "main.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    monkeypatch.setattr(repo, "ping", AsyncMock(return_value=False))
    service = TerminalService(repo, HealthyTerminal(), max_lines=100)

    health = await service.health(auth_mode="none")

    assert health["ok"] is False
    assert health["storage"] == "error"
    assert health["status"] == "failed"
    assert next(c for c in health["components"] if c["id"] == "storage") == {
        "id": "storage",
        "status": "failed",
        "reason": "storage_unavailable",
    }


@pytest.mark.asyncio
async def test_public_health_journal_failure_stays_failed_closed(tmp_path, monkeypatch):
    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    service = TerminalService(repo, HealthyTerminal(), max_lines=100)
    assert service.event_store is not None

    async def broken_event_append(*_args, **_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(service.event_store, "append", broken_event_append)
    health = await service.health("none")
    assert health["ok"] is False
    assert health["storage"] == "error"
    assert health["status"] == "failed"
    component = next(c for c in health["components"] if c["id"] == "storage")
    assert component == {"id": "storage", "status": "failed", "reason": "storage_unavailable"}

"""Public service health over real repository, workflow and event-journal storage."""

import asyncio
import sqlite3

import pytest
from test_sqlite_main_health import _corrupt_instance_events_index

from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_main_db_corruption_propagates_to_public_service_health(tmp_path):
    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    path = tmp_path / "main.sqlite3"
    output = tmp_path / "out.sqlite3"
    repo = SqliteRepository(path, output)
    await repo.initialize()

    def public_service(storage):
        service = TerminalService(storage, HealthyTerminal(), max_lines=100)
        # Exercise the complete default health path, including workflow/events.
        return service

    healthy = await public_service(repo).health(auth_mode="none")
    assert healthy["ok"] is True
    assert healthy["storage"] == "ok"
    assert next(c for c in healthy["components"] if c["id"] == "storage")["status"] == "healthy"

    _corrupt_instance_events_index(path)
    fresh_repo = SqliteRepository(path, output)
    health = await public_service(fresh_repo).health(auth_mode="none")
    assert health["ok"] is False
    assert health["storage"] == "error"
    assert health["status"] == "failed"
    storage_component = next(c for c in health["components"] if c["id"] == "storage")
    assert storage_component == {
        "id": "storage",
        "status": "failed",
        "reason": "storage_unavailable",
    }
    assert health["terminal"]["ok"] is True  # runtime terminal liveness is independent


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


@pytest.mark.asyncio
async def test_public_health_failed_workflow_sql_marks_storage_failed(tmp_path, monkeypatch):
    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    path = tmp_path / "main.sqlite3"
    repo = SqliteRepository(path, tmp_path / "out.sqlite3")
    await repo.initialize()
    service = TerminalService(repo, HealthyTerminal(), max_lines=100)

    async def broken_workflow():
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(service.task_coordinator, "health", broken_workflow)
    result = await service.health("none")
    assert result["ok"] is False
    assert result["storage"] == "error"
    assert result["status"] == "failed"
    assert result["workflow"]["ok"] is False
    assert next(c for c in result["components"] if c["id"] == "storage")["status"] == "failed"


@pytest.mark.asyncio
async def test_public_health_budget_timeout_keeps_complete_failure_contract(tmp_path, monkeypatch):
    import terminal_mcp.core.service as service_module
    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    repo = SqliteRepository(tmp_path / "main.sqlite3", tmp_path / "out.sqlite3")
    await repo.initialize()
    service = TerminalService(repo, HealthyTerminal(), max_lines=100)

    async def never_finishes_in_budget():
        await asyncio.sleep(0.1)
        return True

    monkeypatch.setattr(repo, "ping", never_finishes_in_budget)
    monkeypatch.setattr(service_module, "HEALTH_TIMEOUT_SECONDS", 0.01)
    health = await service.health("none")
    assert health["ok"] is False
    assert health["storage"] == "error"
    assert health["status"] == "failed"
    assert health["components"]
    assert next(c for c in health["components"] if c["id"] == "storage") == {
        "id": "storage",
        "status": "failed",
        "reason": "storage_unavailable",
    }

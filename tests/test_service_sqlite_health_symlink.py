"""Service/storage integration requiring real symlink creation and atomic replacement."""

import os

import pytest
from test_sqlite_main_health import _corrupt_instance_events_index

from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_public_health_rechecks_database_after_symlink_target_switch(tmp_path):
    from terminal_mcp.core.service import TerminalService

    class HealthyTerminal:
        async def health(self):
            return {"ok": True, "degraded": False, "worker_health": {"1": True}}

    good = tmp_path / "good.sqlite3"
    broken = tmp_path / "broken.sqlite3"
    for path in (good, broken):
        await SqliteRepository(path, tmp_path / f"{path.stem}-out.sqlite3").initialize()
    _corrupt_instance_events_index(broken)

    link = tmp_path / "active.sqlite3"
    link.symlink_to(good)
    repo = SqliteRepository(link, tmp_path / "active-out.sqlite3")
    service = TerminalService(repo, HealthyTerminal(), max_lines=100)
    first = await service.health("none")
    assert first["ok"] is True
    assert first["status"] == "healthy"

    replacement = tmp_path / "replacement-link"
    replacement.symlink_to(broken)
    os.replace(replacement, link)
    after = await service.health("none")  # public path must recheck directly
    assert after["ok"] is False
    assert await repo.ping() is False  # target-aware integrity cache
    assert after["storage"] == "error"
    assert after["status"] == "failed"
    assert next(c for c in after["components"] if c["id"] == "storage") == {
        "id": "storage",
        "status": "failed",
        "reason": "storage_unavailable",
    }

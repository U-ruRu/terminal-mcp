"""Repository target identity requiring real symlink creation and atomic replacement."""

import os

import pytest
from test_sqlite_main_health import _corrupt_instance_events_index

from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_main_db_health_symlink_switch_during_probe_fails_closed(tmp_path, monkeypatch):
    good = tmp_path / "good.sqlite3"
    broken = tmp_path / "broken.sqlite3"
    for path in (good, broken):
        await SqliteRepository(path, tmp_path / f"{path.stem}-out.sqlite3").initialize()
    _corrupt_instance_events_index(broken)
    link = tmp_path / "active.sqlite3"
    link.symlink_to(good)
    repo = SqliteRepository(link, tmp_path / "out.sqlite3")
    probe = repo._health_integrity_probe

    async def switch_during_probe(**kwargs):
        result = await probe(**kwargs)
        replacement = tmp_path / "replacement-link"
        replacement.symlink_to(broken)
        os.replace(replacement, link)
        return result

    monkeypatch.setattr(repo, "_health_integrity_probe", switch_during_probe)
    assert await repo.ping() is False  # never publish old-target success after retarget
    monkeypatch.setattr(repo, "_health_integrity_probe", probe)
    assert await repo.ping() is False

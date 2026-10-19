"""The offline release path preserves old state without starting runtime workers."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import initialize_fresh_identity
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.config import Settings
from terminal_mcp.deployment.release import migrate_stores
from terminal_mcp.deployment.update import snapshot
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
async def test_offline_release_preserves_registration_and_application_rows(tmp_path, monkeypatch):
    import terminal_mcp.app as app_module

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.db",
        auth_database_path=tmp_path / "auth.db",
        output_cache_path=tmp_path / "output.db",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "install.env",
        log_path=tmp_path / "runtime.log",
        cwd=tmp_path,
        metrics_port=0,
        fleet_instance_id="a",
    )
    await SqliteRepository(settings.database_path, settings.output_cache_path).initialize()
    await AuthFoundationStore(settings.auth_database_path).initialize()
    await OAuthStore(settings.auth_database_path).initialize()
    await PairingStore(settings.auth_database_path).initialize()
    initialize_fresh_identity(settings.database_path, settings.auth_database_path)
    started = datetime.now(UTC).isoformat()
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "INSERT INTO agent_sessions(agent_id,registered_at,last_activity_at,task_summary,"
            "intent,work_scope,state,source_instance_id,global_expires_at) "
            "VALUES('old-local',?,?,'keep task','keep intent','workspace','active','a',?)",
            (started, started, deadline),
        )
    await TaskStore(settings.database_path).create_task(
        "default", "retained", "Keep application result"
    )
    profile = {
        "stores": {
            "runtime": str(settings.database_path),
            "output": str(settings.output_cache_path),
            "auth": str(settings.auth_database_path),
            "fleet_control": str(settings.effective_fleet_control_path()),
        }
    }
    backup = tmp_path / "snapshot"
    snapshot(profile, backup, [])
    original_factory = app_module.create_app
    captured = []

    def factory():
        app = original_factory(settings)
        captured.append(app)
        return app

    monkeypatch.setattr(app_module, "create_app", factory)
    versions = await migrate_stores(profile, backup)
    assert versions["runtime"] >= 26
    state = captured[0].state
    slot = state.access_mesh.store.slot_for_agent("old-local")
    assert slot is not None and slot.kind == "temporary"
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT title FROM work_items").fetchone() == (
            "Keep application result",
        )
        assert db.execute(
            "SELECT hard_expires_at FROM logical_agent_work_sessions "
            "WHERE logical_agent_id='old-local'"
        ).fetchone() == (deadline,)
        assert db.execute("SELECT state FROM agent_sessions").fetchone() == ("forced",)
    assert state.access_mesh_replication._task is None
    projection = state.fleet_projection_service
    assert projection is None or projection._task is None
    with sqlite3.connect(settings.effective_fleet_control_path()) as db:
        db.execute("UPDATE fleet_members SET state='draining',updated_at=?", (started,))
        original_members = db.execute("SELECT * FROM fleet_members").fetchall()
    next_backup = tmp_path / "next-snapshot"
    snapshot(profile, next_backup, [])
    await migrate_stores(profile, next_backup)
    with sqlite3.connect(settings.effective_fleet_control_path()) as db:
        assert db.execute("SELECT * FROM fleet_members").fetchall() == original_members

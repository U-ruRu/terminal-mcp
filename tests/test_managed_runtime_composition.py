"""Real application composition and lifespan acceptance for managed recovery."""

from fastapi.testclient import TestClient
from persistent_app_test_helpers import persistent_app_with_execution_port as create_app

from terminal_mcp.application.sessions import SessionApplication
from terminal_mcp.config import Settings
from terminal_mcp.storage.work_windows import WorkWindowStore


def _settings(tmp_path, **overrides):
    values = dict(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal-mcp.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        mcp_auth_mode="bearer",
        actions_auth_mode="bearer",
        bearer_tokens="console-token",
    )
    values.update(overrides)
    return Settings(**values)


def test_create_app_composes_managed_runtime_without_public_cutover(tmp_path):
    app = create_app(_settings(tmp_path, persistent_agents_enabled=True))

    assert isinstance(app.state.persistent_lifecycle.store, WorkWindowStore)
    assert app.state.application.managed_identity is app.state.managed_identity
    assert app.state.application.managed_sessions is app.state.managed_sessions
    assert isinstance(app.state.application.sessions, SessionApplication)
    assert app.state.application.sessions is not app.state.managed_sessions
    assert app.state.managed_runtime.enabled
    assert not app.state.managed_runtime.running

    with TestClient(app):
        assert app.state.managed_runtime.running

    assert not app.state.managed_runtime.running


def test_create_app_keeps_managed_recovery_disabled_with_persistent_feature_off(tmp_path):
    app = create_app(_settings(tmp_path, persistent_agents_enabled=False))
    assert not app.state.managed_runtime.enabled
    assert not app.state.managed_runtime.running

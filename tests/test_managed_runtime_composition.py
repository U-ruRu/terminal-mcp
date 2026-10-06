from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.application.managed_runtime import ManagedSessionRuntime
from terminal_mcp.application.sessions import SessionApplication
from terminal_mcp.config import Settings
from terminal_mcp.storage.work_windows import WorkWindowStore


class _Recovery:
    def __init__(self, *, failures=0):
        self.calls = 0
        self.failures = failures

    async def tick(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient")


class _Port:
    pass


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


@pytest.mark.asyncio
async def test_managed_runtime_is_single_bounded_host_task_and_survives_tick_failure():
    recovery = _Recovery(failures=1)
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=True)

    await runtime.start(interval_seconds=0.2)
    assert runtime.running
    # Initial failure is contained; the next durable pass still runs.
    await asyncio.sleep(0.24)
    assert recovery.calls >= 2
    await runtime.stop()
    assert not runtime.running

    calls = recovery.calls
    await asyncio.sleep(0.22)
    assert recovery.calls == calls


@pytest.mark.asyncio
async def test_managed_runtime_disabled_never_starts_recovery():
    recovery = _Recovery()
    runtime = ManagedSessionRuntime(_Port(), _Port(), recovery, enabled=False)
    await runtime.start()
    assert not runtime.running
    assert recovery.calls == 0


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

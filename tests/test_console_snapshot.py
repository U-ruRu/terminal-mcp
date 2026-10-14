from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=False,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        actions_auth_mode="bearer",
        bearer_tokens="console-token",
    )


def test_console_snapshot_cursor_first_contract_replays_concurrent_changes(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.portal is not None
        before = client.portal.call(app.state.service.event_store.high_water_seq)
        assert before == 0

        async def capture_snapshot():
            return await app.state.service.console_snapshot(
                "bearer",
                public_base_url="https://terminal.example",
            )

        snapshot = client.portal.call(capture_snapshot)
        assert snapshot["ok"] is True
        assert snapshot["high_water_seq"] == before

        async def read_replay():
            return await app.state.service.event_store.read(
                since=snapshot["high_water_seq"], limit=100
            )

        replay = client.portal.call(read_replay)
        assert replay["gap"] is False
        assert any(event["event_type"] == "health.changed" for event in replay["events"])
        assert replay["high_water_seq"] > snapshot["high_water_seq"]


def test_console_snapshot_starts_independent_read_models_concurrently(tmp_path, monkeypatch):
    import asyncio

    app = create_app(settings(tmp_path))
    service = app.state.service
    started = set()
    all_started = asyncio.Event()

    async def synchronize(name):
        started.add(name)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)

    original_health = service.health
    original_context = service.context
    original_overview = service.agent_coordinator.overview

    async def health(*args, **kwargs):
        await synchronize("health")
        return await original_health(*args, **kwargs)

    async def context(*args, **kwargs):
        await synchronize("context")
        return await original_context(*args, **kwargs)

    async def overview(*args, **kwargs):
        if kwargs.get("show_details") is True:
            await synchronize("agents")
        return await original_overview(*args, **kwargs)

    monkeypatch.setattr(service, "health", health)
    monkeypatch.setattr(service, "context", context)
    monkeypatch.setattr(service.agent_coordinator, "overview", overview)

    with TestClient(app) as client:
        response = client.get(
            "/actions/console/snapshot",
            headers={"Authorization": "Bearer console-token"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert started == {"health", "context", "agents"}


def test_authorized_console_snapshot_has_expected_read_models(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        result = client.get(
            "/actions/console/snapshot",
            headers={"Authorization": "Bearer console-token"},
        )
    assert result.status_code == 200
    assert {"instance", "agents", "tasks", "contexts", "communications", "consistency"} <= set(
        result.json()
    )

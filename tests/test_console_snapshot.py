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
        access_mesh_enabled=True,
        fleet_instance_id="test-node",
        fleet_signing_private_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        fleet_peers_json="[]",
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=True,
        legacy_agent_admission_enabled=True,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        auth_mode="bearer",
        actions_auth_mode="bearer",
        bearer_tokens="console-token",
    )


def test_console_snapshot_captures_cursor_before_read_models(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        before = client.portal.call(app.state.service.event_store.high_water_seq)

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
        assert any(event["event_type"] == "health.changed" for event in replay["events"])
        assert replay["high_water_seq"] > snapshot["high_water_seq"]


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

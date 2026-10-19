"""Native Windows app, authentication and Standalone Console wiring."""

import base64

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings
from terminal_mcp.deployment.windows import configure_browser_origins


@pytest.mark.parametrize("origin", ["https://localhost", "https://terminal-console.solvenger.app"])
def test_paired_windows_node_exposes_current_standalone_console(tmp_path, origin):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        fleet_node_meta_path=tmp_path / "node-meta.sqlite3",
        fleet_control_path=tmp_path / "control.sqlite3",
        fleet_projection_path=tmp_path / "projection.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "config/terminal-mcp.env",
        log_path=tmp_path / "events.log",
        cwd=tmp_path,
        metrics_port=0,
        auth_mode="oauth",
        oauth_signing_secret="windows-acceptance-signing-key-local-only",
        public_base_url="https://windows.example",
    )
    settings.env_file_path.parent.mkdir()
    settings.env_file_path.write_text('TERMINAL_MCP_PUBLIC_BASE_URL="https://windows.example"\n')
    configure_browser_origins(tmp_path)
    installed = Settings(_env_file=settings.env_file_path)
    settings.console_allowed_origins = installed.console_allowed_origins
    app = create_app(settings)
    with TestClient(app) as client:
        code = client.portal.call(app.state.pairing_store.create)
        exchange = client.post(
            "/pairing/exchange",
            json={
                "code": code,
                "public_key": base64.b64encode(bytes(range(32))).decode(),
                "device_label": "Windows acceptance device",
            },
        )
        assert exchange.status_code == 200
        headers = {"Authorization": "Bearer " + exchange.json()["access_token"], "Origin": origin}
        for path in (
            "/actions/fleet/control",
            "/actions/console/snapshot",
            "/actions/access/slots",
        ):
            preflight = client.options(
                path,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
            assert preflight.status_code == 204
            assert preflight.headers["access-control-allow-origin"] == origin
        control = client.get("/actions/fleet/control", headers=headers)
        snapshot = client.get("/actions/console/snapshot", headers=headers)
        defaults = client.get("/actions/access/defaults", headers=headers)
        slots = client.get("/actions/access/slots", headers=headers)
        ready = client.get("/health/ready")
    assert (
        control.status_code
        == snapshot.status_code
        == defaults.status_code
        == ready.status_code
        == 200
    )
    assert control.json()["control"]["native_access_trust"]["state"] == "autonomous"
    assert slots.status_code == 200 and slots.json()["ok"] is True
    for response in (control, snapshot, defaults, slots):
        assert response.headers["access-control-allow-origin"] == origin
    resources = snapshot.json()["instance"]["resources"]
    assert resources["status"] == "available"
    for kind in ("cpu", "memory", "filesystem", "uptime"):
        assert resources[kind]["status"] == "available"
    assert resources["cpu"]["logical_cores"] > 0
    assert resources["memory"]["total_bytes"] > 0
    assert resources["filesystem"]["total_bytes"] > 0
    assert resources["uptime"]["seconds"] > 0
    assert ready.json()["ready"] is True

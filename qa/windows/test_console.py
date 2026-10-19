"""Native Windows app, authentication and Standalone Console wiring."""

import base64

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def test_paired_windows_node_exposes_current_standalone_console(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "server.env",
        log_path=tmp_path / "events.log",
        cwd=tmp_path,
        metrics_port=0,
        auth_mode="oauth",
        oauth_signing_secret="windows-acceptance-signing-key-local-only",
        public_base_url="https://windows.example",
    )
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
        headers = {"Authorization": "Bearer " + exchange.json()["access_token"]}
        control = client.get("/actions/fleet/control", headers=headers)
        snapshot = client.get("/actions/console/snapshot", headers=headers)
        defaults = client.get("/actions/access/defaults", headers=headers)
        ready = client.get("/health/ready")
    assert (
        control.status_code
        == snapshot.status_code
        == defaults.status_code
        == ready.status_code
        == 200
    )
    assert control.json()["control"]["native_access_trust"]["state"] == "autonomous"
    assert ready.json()["ready"] is True

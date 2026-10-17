import json
import re

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def make_settings(tmp_path):
    return Settings(
        _env_file=None,
        _env_prefix=f"TMCP_OAUTH_TEST_{tmp_path.name.upper()}_",
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        cwd="/",
        terminal_user="root",
        env_file_path=tmp_path / "terminal-mcp.env",
        public_base_url="https://testserver",
        admin_username="operator",
        admin_password="secret",
        admin_session_secret="s" * 32,
        auth_mode="bearer",
        bearer_tokens="api-token",
        metrics_port=0,
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=False,
        legacy_agent_admission_enabled=True,
    )


def login(client):
    login_page = client.get("/admin/login").text
    login_csrf = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', login_page).group(1)
    response = client.post(
        "/admin/login",
        data={"username": "operator", "password": "secret", "csrf_token": login_csrf},
    )
    assert response.status_code == 200
    page = client.get("/admin").text
    return re.search(r'name="csrf_token" value="([a-f0-9]+)"', page).group(1)


def test_admin_saves_runtime_configuration(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        csrf = login(client)
        updated = client.post(
            "/admin/runtime",
            data={
                "cwd": "/",
                "user": "root",
                "health_command": "printf admin-health",
                "csrf_token": csrf,
            },
        )
    assert updated.status_code == 200
    values = {
        key: json.loads(value)
        for key, value in (
            line.split("=", 1) for line in (tmp_path / "terminal-mcp.env").read_text().splitlines()
        )
    }
    assert values["TERMINAL_MCP_HEALTH_COMMAND"] == "printf admin-health"

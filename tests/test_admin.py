import json
import os
import re

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def make_settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        cwd="/",
        terminal_user="root",
        env_file_path=tmp_path / "terminal-mcp.env",
        public_base_url="https://testserver",
        admin_username="operator",
        admin_password="secret",
        admin_session_secret="s" * 32,
        auth_mode="bearer",
        bearer_tokens="api-token",
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


def test_admin_credentials_runtime_and_privacy(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/privacy").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        csrf = login(client)
        client.post("/admin/bearer", data={"name": "denied", "expires_at": ""})
        assert "denied" not in client.get("/admin").text
        added = client.post(
            "/admin/bearer",
            data={"name": "client", "expires_at": "2099-01-01T00:00:00+00:00", "csrf_token": csrf},
        )
        assert added.status_code == 200
        page = client.get("/admin").text
        assert "client" in page and "tmcp_" in page and "operator" in page and "secret" in page
        env = tmp_path / "terminal-mcp.env"
        assert env.exists() and oct(os.stat(env).st_mode & 0o777) == "0o600"
        values = {
            line.split("=", 1)[0]: json.loads(line.split("=", 1)[1])
            for line in env.read_text().splitlines()
        }
        assert "client" in values["TERMINAL_MCP_BEARER_CREDENTIALS_JSON"]
        client.post(
            "/admin/oauth-user",
            data={
                "username": "agent",
                "password": "plain-pass",
                "expires_at": "",
                "csrf_token": csrf,
            },
        )
        assert "plain-pass" in client.get("/admin").text
        updated = client.post(
            "/admin/runtime",
            data={
                "cwd": "/",
                "user": "root",
                "health_command": "printf 'admin-health\n'",
                "csrf_token": csrf,
            },
        )
        assert updated.status_code == 200
        assert "admin-health" in client.get("/admin").text
        values = {
            line.split("=", 1)[0]: json.loads(line.split("=", 1)[1])
            for line in env.read_text().splitlines()
        }
        assert values["TERMINAL_MCP_HEALTH_COMMAND"] == "printf 'admin-health\n'"
        health = client.get("/actions/health", headers={"Authorization": "Bearer api-token"}).json()
        assert health["custom_command"]["lines"][0].endswith("admin-health")


def test_rate_limit_login(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        token = re.search(
            r'name="csrf_token" value="([A-Za-z0-9_-]+)"', client.get("/admin/login").text
        ).group(1)
        statuses = [
            client.post(
                "/admin/login",
                data={"username": "bad", "password": "bad", "csrf_token": token},
            ).status_code
            for _ in range(6)
        ]
        assert statuses[-1] == 429

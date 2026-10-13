"""Fixtures for the HTTP authentication acceptance scenarios."""

import base64
import hashlib

import pytest

import terminal_mcp.auth.credentials as credential_module
from terminal_mcp.config import Settings


def pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def settings(tmp_path, **overrides):
    data = dict(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
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
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="test-secret-that-is-at-least-32-bytes",
        oauth_admin_username="admin",
        oauth_admin_password="secret",
        oauth_users_json="[]",
        bearer_credentials_json="[]",
        mcp_auth_mode="",
        actions_auth_mode="",
    )
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def file_oauth_credentials(monkeypatch):
    values = {
        credential_module.OAUTH_USERNAME_FILE: b"admin",
        credential_module.OAUTH_PASSWORD_FILE: b"secret",
    }

    def configure(username: str = "admin", password: str = "secret"):
        values[credential_module.OAUTH_USERNAME_FILE] = username.encode()
        values[credential_module.OAUTH_PASSWORD_FILE] = password.encode()

    def read(path, label):
        del label
        return values[path]

    monkeypatch.setattr(credential_module, "_read_oauth_credential", read)
    return configure


def start_agent(client, headers):
    plan = {
        "task_summary": "HTTP test",
        "intent": "Exercise actions",
        "details": ["Exercise actions", "Verify actions"],
        "work_scope": ["repo:tests"],
    }
    proposed = client.post(
        "/actions/agent/start",
        json=plan,
        headers=headers,
    )
    assert proposed.status_code == 200
    proposal = proposed.json()
    assert proposal["admission_required"] is True

    response = client.post(
        "/actions/agent/start",
        json={"agent_id": proposal["proposed_agent_id"], **plan},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["self"]["agent_id"]


@pytest.fixture
def bearer_client(tmp_path):
    from fastapi.testclient import TestClient

    from terminal_mcp.app import create_app

    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha,beta"))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def bearer_session(bearer_client):
    headers = {"Authorization": "Bearer alpha"}
    return bearer_client, headers, start_agent(bearer_client, headers)

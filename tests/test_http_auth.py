"""HTTP authentication and action wiring across the real application and stores."""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from auth_http_test_helpers import (
    bearer_client as bearer_client,
)
from auth_http_test_helpers import (
    bearer_session as bearer_session,
)
from auth_http_test_helpers import (
    file_oauth_credentials as file_oauth_credentials,
)
from auth_http_test_helpers import (
    pkce,
    settings,
    start_agent,
)
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app


def test_bearer_task_creation_requires_isolation(bearer_session):
    client, headers, agent_id = bearer_session
    missing_isolation = client.post(
        "/actions/task",
        json={
            "agent_id": agent_id,
            "action": "create",
            "namespace": "http",
            "task_id": "TASK-MISSING-HINT",
            "title": "missing isolation hint",
        },
        headers=headers,
    )
    assert missing_isolation.status_code == 422


def test_bearer_message_allows_implicit_broadcast(bearer_session):
    client, headers, agent_id = bearer_session
    implicit_broadcast = client.post(
        "/actions/message",
        json={"agent_id": agent_id, "text": "ordinary broadcast"},
        headers=headers,
    )
    assert implicit_broadcast.status_code == 200


def test_bearer_alert_requires_explicit_destination(bearer_session):
    client, headers, agent_id = bearer_session
    omitted_alert = client.post(
        "/actions/message",
        json={"agent_id": agent_id, "text": "unsafe", "alert": True},
        headers=headers,
    )
    assert omitted_alert.status_code == 422
    assert "message.alert: ALERT requires an explicit destination" in omitted_alert.text


def test_bearer_task_message_targets_durable_task(bearer_session):
    client, headers, agent_id = bearer_session
    created_task = client.post(
        "/actions/task",
        json={
            "agent_id": agent_id,
            "action": "create",
            "isolation_hint": "none",
            "namespace": "http",
            "task_id": "TASK-1",
            "title": "HTTP task target",
        },
        headers=headers,
    )
    assert created_task.status_code == 200 and created_task.json()["ok"] is True
    task_message = client.post(
        "/actions/message",
        json={
            "agent_id": agent_id,
            "text": "Durable task note",
            "namespace": "http",
            "task_id": "TASK-1",
        },
        headers=headers,
    )
    assert task_message.status_code == 200
    assert task_message.json()["namespace"] == "http"
    assert task_message.json()["task_id"] == "TASK-1"
    assert task_message.json()["delivered_to"] == []


@pytest.mark.parametrize("intent_length,status", [(160, 200), (161, 422)])
def test_bearer_coordinate_enforces_intent_bound(bearer_session, intent_length, status):
    client, headers, agent_id = bearer_session
    response = client.post(
        "/actions/coordinate",
        json={"agent_id": agent_id, "step": 1, "intent": "x" * intent_length},
        headers=headers,
    )
    assert response.status_code == status
    if status == 200:
        assert response.json()["intent"] == "x" * intent_length


def test_bearer_missing_command_cancel_preserves_anonymous_identity(bearer_client):
    client = bearer_client
    headers = {"Authorization": "Bearer alpha"}
    anonymous_cancel = client.post(
        "/actions/cancel", json={"cmd_hash": "deadbeef"}, headers=headers
    )
    assert anonymous_cancel.status_code == 200
    assert anonymous_cancel.json()["agent_name"] == "anonymous"


def test_bearer_recovery_rejects_unhandled_controls(bearer_session):
    client, headers, agent_id = bearer_session
    rejected = client.post(
        "/actions/recovery",
        json={"agent_id": agent_id, "cmd": "printf old", "timeout_ms": 1000},
        headers=headers,
    )
    assert rejected.status_code == 422


def test_oauth_pkce_refresh_and_protected_action(tmp_path, file_oauth_credentials):
    app = create_app(settings(tmp_path, auth_mode="oauth"))
    with TestClient(app, follow_redirects=False) as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["scopes_supported"] == ["terminal:read", "terminal:execute"]
        resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource == client.get("/mcp/.well-known/oauth-protected-resource").json()
        assert resource["resource"] == "https://terminal.example/mcp"
        for role in ("executor", "coordinator"):
            role_path = f"/terminal-mcp/{role}/v1/mcp"
            role_metadata = client.get(f"/.well-known/oauth-protected-resource{role_path}")
            assert role_metadata.status_code == 200
            assert role_metadata.json()["resource"] == f"https://terminal.example{role_path}"
            embedded = client.get(f"{role_path}/.well-known/oauth-protected-resource")
            assert embedded.status_code == 200
            assert embedded.json() == role_metadata.json()
            unauthorized = client.post(f"{role_path}/")
            assert unauthorized.status_code == 401
            assert unauthorized.headers["www-authenticate"] == (
                'Bearer resource_metadata="'
                f"https://terminal.example/.well-known/oauth-protected-resource{role_path}"
                '"'
            )
        reg = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chat.example/callback"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg.status_code == 201
        registration = reg.json()
        assert registration["client_id_issued_at"] > 0
        assert registration["client_secret_expires_at"] == 0
        assert registration["grant_types"] == ["authorization_code", "refresh_token"]
        client_id = registration["client_id"]
        verifier = "v" * 64
        authorize_params = {
            "client_id": client_id,
            "redirect_uri": "https://chat.example/callback",
            "response_type": "code",
            "scope": "terminal:read terminal:execute",
            "state": "abc",
            "code_challenge": pkce(verifier),
            "code_challenge_method": "S256",
        }
        authorize_form = client.get("/oauth/authorize", params=authorize_params)
        assert authorize_form.status_code == 200

        authorize_data = {
            key: value for key, value in authorize_params.items() if key != "response_type"
        }
        denied = client.post(
            "/oauth/authorize",
            data={**authorize_data, "username": "admin", "password": "wrong"},
        )
        assert denied.status_code == 403

        authorize = client.post(
            "/oauth/authorize",
            data={**authorize_data, "username": "admin", "password": "secret"},
        )
        assert authorize.status_code == 303
        code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

        token_data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": "https://chat.example/callback",
            "code_verifier": verifier,
        }
        issued = client.post("/oauth/token", data=token_data)
        assert issued.status_code == 200
        tokens = issued.json()
        reused_code = client.post("/oauth/token", data=token_data)
        assert reused_code.status_code == 400
        assert reused_code.json() == {"error": "invalid_grant"}

        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        start_agent(client, headers)
        assert client.get("/actions/health", headers=headers).status_code == 200
        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert refreshed.status_code == 200
        reused = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert reused.status_code == 400
        asyncio.run(app.state.oauth_store.delete_client(client_id))
        assert client.get("/actions/health", headers=headers).status_code == 401


def test_same_oauth_user_can_authorize_multiple_clients(tmp_path, file_oauth_credentials):
    file_oauth_credentials("shared", "shared-secret")
    app = create_app(
        settings(
            tmp_path,
            auth_mode="oauth",
            admin_username="stale-admin",
            admin_password="stale-secret",
            oauth_users_json='[{"username":"stale-user","password":"stale-secret"}]',
        )
    )
    with TestClient(app, follow_redirects=False) as client:
        registrations = []
        for index in (1, 2):
            response = client.post(
                "/oauth/register",
                json={
                    "client_name": f"Client {index}",
                    "redirect_uris": [f"https://client{index}.example/callback"],
                    "token_endpoint_auth_method": "none",
                },
            )
            assert response.status_code == 201
            registrations.append(response.json())

        access_tokens = []
        for index, registration in enumerate(registrations, start=1):
            verifier = str(index) * 64
            redirect_uri = f"https://client{index}.example/callback"
            authorize = client.post(
                "/oauth/authorize",
                data={
                    "client_id": registration["client_id"],
                    "redirect_uri": redirect_uri,
                    "scope": "terminal:read",
                    "state": f"state-{index}",
                    "code_challenge": pkce(verifier),
                    "code_challenge_method": "S256",
                    "username": "shared",
                    "password": "shared-secret",
                },
            )
            assert authorize.status_code == 303
            code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]
            issued = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": registration["client_id"],
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            assert issued.status_code == 200
            access_tokens.append(issued.json()["access_token"])

        assert access_tokens[0] != access_tokens[1]
        for token in access_tokens:
            headers = {"Authorization": f"Bearer {token}"}
            agent_id = start_agent(client, headers)
            assert (
                client.get(
                    "/actions/health", params={"agent_id": agent_id}, headers=headers
                ).status_code
                == 200
            )

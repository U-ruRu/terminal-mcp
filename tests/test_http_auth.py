import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def settings(tmp_path, **overrides):
    data = dict(
        database_path=tmp_path / "db.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
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


def start_agent(client, headers):
    response = client.post(
        "/actions/agent/start",
        json={
            "task_summary": "HTTP test",
            "intent": "Exercise actions",
            "details": ["Exercise actions", "Verify actions"],
            "work_scope": ["repo:tests"],
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["self"]["agent_id"]


def test_bearer_actions_and_openapi(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha,beta"))
    with TestClient(app) as client:
        assert client.get("/actions/health").status_code == 401
        headers = {"Authorization": "Bearer alpha"}
        agent_id = start_agent(client, headers)
        health = client.get("/actions/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["agent_name"] == "anonymous"

        run = client.post(
            "/actions/run", json={"agent_id": agent_id, "cmd": "printf ok"}, headers=headers
        )
        assert run.status_code == 200 and run.json()["ok"] is True
        cmd_hash = run.json()["cmd_hash"]
        for _ in range(100):
            result = client.post(
                "/actions/read",
                json={"agent_id": agent_id, "cmd_hash": cmd_hash, "lines_count": 100, "offset": 0},
                headers=headers,
            ).json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert result["status"] == "completed"

        schema = client.get("/openapi.json").json()
        expected_paths = {
            "/actions/agent/start",
            "/actions/coordinate",
            "/actions/message",
            "/actions/agents",
            "/actions/agent/finish",
            "/actions/run",
            "/actions/read",
            "/actions/recovery",
            "/actions/cancel",
            "/actions/health",
        }
        assert set(schema["paths"]) == expected_paths
        assert schema["paths"]["/actions/run"]["post"]["operationId"] == "runCommand"
        run_request = schema["components"]["schemas"]["RunRequest"]
        assert set(run_request["required"]) == {"agent_id", "cmd"}
        recovery_request = schema["components"]["schemas"]["RecoveryRequest"]
        assert set(recovery_request["required"]) == {"cmd"}
        cancel_request = schema["components"]["schemas"]["CancelRequest"]
        assert set(cancel_request["required"]) == {"cmd_hash"}
        read_request = schema["components"]["schemas"]["ReadRequest"]
        assert "required" not in read_request or read_request["required"] == []
        health_operation = schema["paths"]["/actions/health"]["get"]
        health_params = health_operation.get("parameters", [])
        assert any(
            item["name"] == "agent_id" and item["required"] is False
            for item in health_params
        )
        start_request = schema["components"]["schemas"]["AgentStartRequest"]
        assert start_request["properties"]["task_summary"]["anyOf"][0]["maxLength"] == 120
        assert start_request["properties"]["details"]["anyOf"][0]["maxItems"] == 12
        details_schema = start_request["properties"]["details"]["anyOf"][0]
        assert details_schema["items"]["maxLength"] == 160
        assert start_request["properties"]["work_scope"]["anyOf"][0]["maxItems"] == 4
        coordinate_request = schema["components"]["schemas"]["CoordinateRequest"]
        assert set(coordinate_request["required"]) == {"agent_id"}
        assert coordinate_request["properties"]["intent"]["anyOf"][0]["maxLength"] == 160
        message_request = schema["components"]["schemas"]["MessageRequest"]
        assert set(message_request["required"]) == {"agent_id"}
        assert message_request["properties"]["message_hash"]["anyOf"][0]["maxLength"] == 8

        accepted_coordinate = client.post(
            "/actions/coordinate",
            json={"agent_id": agent_id, "step": 1, "intent": "x" * 160},
            headers=headers,
        )
        assert accepted_coordinate.status_code == 200
        assert accepted_coordinate.json()["intent"] == "x" * 160
        rejected_coordinate = client.post(
            "/actions/coordinate",
            json={"agent_id": agent_id, "step": 1, "intent": "x" * 161},
            headers=headers,
        )
        assert rejected_coordinate.status_code == 422

        recovery = client.post(
            "/actions/recovery",
            json={"agent_id": agent_id, "cmd": "printf action-recovery"},
            headers=headers,
        ).json()
        assert recovery["ok"] is True
        assert recovery["agent_name"] == agent_id.rsplit("-", 1)[0]
        assert recovery["lines"][0].endswith("action-recovery")

        anonymous_recovery = client.post(
            "/actions/recovery",
            json={"cmd": "printf anonymous-action-recovery"},
            headers=headers,
        ).json()
        assert anonymous_recovery["ok"] is True
        assert anonymous_recovery["agent_name"] == "anonymous"
        assert anonymous_recovery["lines"][0].endswith("anonymous-action-recovery")

        global_read = client.post("/actions/read", json={}, headers=headers)
        assert global_read.status_code == 200
        assert global_read.json()["agent_name"] == "anonymous"
        one_sided_read = client.post(
            "/actions/read", json={"cmd_hash": cmd_hash}, headers=headers
        )
        assert one_sided_read.status_code == 422

        anonymous_cancel = client.post(
            "/actions/cancel", json={"cmd_hash": "deadbeef"}, headers=headers
        )
        assert anonymous_cancel.status_code == 200
        assert anonymous_cancel.json()["agent_name"] == "anonymous"

        rejected = client.post(
            "/actions/recovery",
            json={"agent_id": agent_id, "cmd": "printf old", "timeout_ms": 1000},
            headers=headers,
        )
        assert rejected.status_code == 422
        for item in schema["paths"].values():
            for method, operation in item.items():
                if method in {"get", "post"}:
                    assert operation["x-openai-isConsequential"] is False


def test_oauth_pkce_refresh_and_protected_action(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="oauth"))
    with TestClient(app, follow_redirects=False) as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["code_challenge_methods_supported"] == ["S256"]
        resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource == client.get("/mcp/.well-known/oauth-protected-resource").json()
        assert resource["resource"] == "https://terminal.example/mcp"
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
        assert (
            client.get("/actions/health", headers=headers).status_code
            == 200
        )
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
        assert (
            client.get("/actions/health", headers=headers).status_code
            == 401
        )


def test_same_oauth_user_can_authorize_multiple_clients(tmp_path):
    app = create_app(
        settings(
            tmp_path,
            auth_mode="oauth",
            oauth_users_json='[{"username":"shared","password":"shared-secret"}]',
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
                    "scope": "terminal:read terminal:execute",
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

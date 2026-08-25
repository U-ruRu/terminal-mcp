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


def test_bearer_actions_and_openapi(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha,beta"))
    with TestClient(app) as client:
        assert client.get("/actions/health").status_code == 401
        assert (
            client.get("/actions/health", headers={"Authorization": "Bearer beta"}).status_code
            == 200
        )
        run = client.post(
            "/actions/run", json={"cmd": "printf ok"}, headers={"Authorization": "Bearer alpha"}
        )
        assert run.status_code == 200 and run.json()["ok"] is True
        cmd_hash = run.json()["cmd_hash"]
        for _ in range(100):
            result = client.post(
                "/actions/read",
                json={"cmd_hash": cmd_hash, "lines_count": 100, "offset": 0},
                headers={"Authorization": "Bearer alpha"},
            ).json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        schema = client.get("/openapi.json").json()
        assert schema["servers"] == [{"url": "https://terminal.example"}]
        assert set(schema["paths"]) == {
            "/actions/run",
            "/actions/read",
            "/actions/recovery",
            "/actions/cancel",
            "/actions/health",
        }
        assert schema["paths"]["/actions/run"]["post"]["operationId"] == "runCommand"
        assert schema["paths"]["/actions/read"]["post"]["operationId"] == "readTerminal"
        assert "get" not in schema["paths"]["/actions/read"]
        run_schema = schema["components"]["schemas"]["RunResponse"]
        assert set(run_schema["properties"]) == {"ok", "cmd_hash", "error"}
        read_request = schema["components"]["schemas"]["ReadRequest"]
        assert read_request["properties"]["lines_count"]["default"] == 500
        assert read_request["properties"]["lines_count"]["maximum"] == 1000
        recovery_request = schema["components"]["schemas"]["RecoveryRequest"]
        assert set(recovery_request["properties"]) == {"cmd"}
        expected_models = {
            "/actions/run": "RunResponse",
            "/actions/read": "ReadResponse",
            "/actions/recovery": "RecoveryResponse",
            "/actions/cancel": "CancelResponse",
        }
        expected_models["/actions/health"] = "HealthResponse"
        for path, model in expected_models.items():
            method = "get" if path.endswith("health") else "post"
            response = schema["paths"][path][method]["responses"]["200"]
            assert response["content"]["application/json"]["schema"] == {
                "$ref": f"#/components/schemas/{model}"
            }
        recovery_result = client.post(
            "/actions/recovery",
            json={"cmd": "printf action-recovery"},
            headers={"Authorization": "Bearer alpha"},
        )
        assert recovery_result.status_code == 200
        recovery_body = recovery_result.json()
        assert recovery_body["ok"] is True
        assert len(recovery_body["cmd_hash"]) == 8
        assert recovery_body["overall_lines_count"] == 1
        assert recovery_body["displayed_lines_count"] == 1
        assert recovery_body["lines"][0].endswith("action-recovery")
        assert "status" not in recovery_body

        rejected_recovery = client.post(
            "/actions/recovery",
            json={"cmd": "printf old", "timeout_ms": 1000},
            headers={"Authorization": "Bearer alpha"},
        )
        assert rejected_recovery.status_code == 422
        rejected_read = client.post(
            "/actions/read",
            json={"lines_count": 1001},
            headers={"Authorization": "Bearer alpha"},
        )
        assert rejected_read.status_code == 422
        for item in schema["paths"].values():
            for method, operation in item.items():
                if method in {"get", "post"}:
                    assert operation["x-openai-isConsequential"] is False
        assert schema["paths"]["/actions/run"]["post"]["security"] == [{"BearerAuth": []}]
        challenge = client.get("/actions/health")
        assert (
            'resource_metadata="https://terminal.example/.well-known/oauth-protected-resource/mcp"'
            in challenge.headers["www-authenticate"]
        )


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
        assert authorize_form.headers["cache-control"] == "no-store, max-age=0"
        assert authorize_form.headers["pragma"] == "no-cache"
        assert "event.persisted" in authorize_form.text

        authorize_data = {
            key: value for key, value in authorize_params.items() if key != "response_type"
        }
        denied = client.post(
            "/oauth/authorize",
            data={**authorize_data, "username": "admin", "password": "wrong"},
        )
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store, max-age=0"

        authorize = client.post(
            "/oauth/authorize",
            data={**authorize_data, "username": "admin", "password": "secret"},
        )
        assert authorize.status_code == 303
        code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

        restored_tab = client.get("/oauth/authorize", params=authorize_params)
        assert restored_tab.status_code == 410
        assert restored_tab.headers["cache-control"] == "no-store, max-age=0"
        assert "already been completed" in restored_tab.text
        assert "window.close()" in restored_tab.text

        replayed_form = client.post(
            "/oauth/authorize",
            data={**authorize_data, "username": "admin", "password": "secret"},
        )
        assert replayed_form.status_code == 410
        assert "already been completed" in replayed_form.text

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
            assert (
                client.get(
                    "/actions/health", headers={"Authorization": f"Bearer {token}"}
                ).status_code
                == 200
            )

import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.auth.middleware import AuthMiddleware
from terminal_mcp.auth.storage import OAuthStore
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
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["version"] == "0.10.1"
        assert client.get("/actions/health").status_code == 401
        headers = {"Authorization": "Bearer alpha"}
        agent_id = start_agent(client, headers)
        health = client.get("/actions/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["agent_name"] == "anonymous"
        assert health.json()["version"] == "0.10.1"

        empty_context = client.post("/actions/context", json={"action": "list"}, headers=headers)
        assert empty_context.status_code == 200
        assert empty_context.json() == {"ok": True, "primary": [], "additional": []}
        primary_context = client.post(
            "/actions/context",
            json={
                "action": "create",
                "summary": "Git workflow",
                "content": "Use the local Git wrapper.",
                "primary": True,
            },
            headers=headers,
        )
        assert primary_context.status_code == 200
        primary_id = primary_context.json()["entry"]["id"]
        additional_context = client.post(
            "/actions/context",
            json={
                "action": "create",
                "summary": "Docs",
                "content": "Read local docs.",
                "primary": False,
            },
            headers=headers,
        )
        additional_id = additional_context.json()["entry"]["id"]
        compact_context = client.post(
            "/actions/context", json={"action": "list"}, headers=headers
        ).json()
        assert compact_context["primary"] == [
            {"id": primary_id, "summary": "Git workflow"}
        ]
        assert compact_context["additional"] == [
            {"id": additional_id, "summary": "Docs"}
        ]
        detailed_context = client.post(
            "/actions/context",
            json={"action": "list", "show_details": True},
            headers=headers,
        ).json()
        assert detailed_context["primary"][0]["content"] == "Use the local Git wrapper."
        assert set(detailed_context["primary"][0]) == {"id", "summary", "content"}
        invalid_list = client.post(
            "/actions/context",
            json={"action": "list", "summary": "invalid"},
            headers=headers,
        )
        assert invalid_list.status_code == 422
        invalid_delete = client.post(
            "/actions/context",
            json={"action": "delete", "id": primary_id, "summary": "invalid"},
            headers=headers,
        )
        assert invalid_delete.status_code == 422
        updated_context = client.post(
            "/actions/context",
            json={"action": "update", "id": additional_id, "primary": True},
            headers=headers,
        ).json()
        assert updated_context["entry"]["primary"] is True
        deleted_context = client.post(
            "/actions/context",
            json={"action": "delete", "id": primary_id},
            headers=headers,
        ).json()
        assert deleted_context == {"ok": True, "deleted_id": primary_id}

        run = client.post(
            "/actions/run",
            json={"agent_id": agent_id, "cmd": "printf ok", "task_scope": "none"},
            headers=headers,
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
            "/actions/context",
            "/actions/tasks",
            "/actions/task",
            "/actions/run",
            "/actions/read",
            "/actions/recovery",
            "/actions/cancel",
            "/actions/health",
        }
        assert set(schema["paths"]) == expected_paths
        assert schema["info"]["version"] == "0.10.1"
        assert schema["paths"]["/actions/run"]["post"]["operationId"] == "runCommand"
        run_request = schema["components"]["schemas"]["RunRequest"]
        assert set(run_request["required"]) == {"agent_id", "cmd", "task_scope"}
        recovery_request = schema["components"]["schemas"]["RecoveryRequest"]
        assert set(recovery_request["required"]) == {"cmd"}
        cancel_request = schema["components"]["schemas"]["CancelRequest"]
        assert set(cancel_request["required"]) == {"cmd_hash"}
        read_request = schema["components"]["schemas"]["ReadRequest"]
        assert "required" not in read_request or read_request["required"] == []
        health_operation = schema["paths"]["/actions/health"]["get"]
        health_params = health_operation.get("parameters", [])
        assert any(
            item["name"] == "agent_id" and item["required"] is False for item in health_params
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
        assert message_request["properties"]["namespace"]["anyOf"][0]["maxLength"] == 120
        assert message_request["properties"]["task_id"]["anyOf"][0]["maxLength"] == 120
        assert "explicit destination" in message_request["properties"]["alert"]["description"]
        context_request = schema["components"]["schemas"]["ContextRequest"]
        assert context_request["required"] == ["action"]
        assert set(context_request["properties"]["action"]["enum"]) == {
            "list",
            "create",
            "update",
            "delete",
        }
        assert context_request["properties"]["summary"]["anyOf"][0]["maxLength"] == 100
        assert context_request["properties"]["id"]["anyOf"][0]["minimum"] == 1
        tasks_request = schema["components"]["schemas"]["TasksRequest"]
        assert tasks_request["properties"]["lane"]["anyOf"][0]["enum"] == [
            "implementation",
            "review",
            "release",
            "integration",
            "general",
        ]
        assert tasks_request["properties"]["state"]["anyOf"][0]["enum"] == [
            "ready",
            "blocked",
            "deferred",
            "done",
        ]
        task_request = schema["components"]["schemas"]["TaskRequest"]
        assert set(task_request["properties"]["action"]["enum"]) == {
            "create",
            "claim",
            "release",
            "update",
            "checkpoint",
            "comment",
            "relate",
            "unrelate",
            "state",
            "done",
            "archive",
        }
        assert task_request["properties"]["priority"]["anyOf"][0]["enum"] == [
            "P0",
            "P1",
            "P2",
            "P3",
        ]
        for legacy_field in ("review_requirements", "dimensions", "verdict", "evidence"):
            assert legacy_field not in task_request["properties"]
        assert "tags" in task_request["properties"]
        assert "force" in task_request["properties"]
        assert "force_reason" in task_request["properties"]
        assert "claim or terminal completion" in task_request["properties"]["force"]["description"]
        isolation_hint = task_request["properties"]["isolation_hint"]["anyOf"][0]
        assert isolation_hint["minLength"] == 1
        assert isolation_hint["maxLength"] == 160
        assert (
            "Required for action=create"
            in task_request["properties"]["isolation_hint"]["description"]
        )
        for field in (
            "claim_intent",
            "blocker_reason",
            "release_reason",
            "archive_note",
            "comment_text",
            "relation_kind",
            "related_namespace",
            "related_task_id",
        ):
            assert field in task_request["properties"]
        claim_intent = task_request["properties"]["claim_intent"]["anyOf"][0]
        assert claim_intent["maxLength"] == 160
        assert "tags" in schema["components"]["schemas"]["TasksRequest"]["properties"]
        assert "task_scope" in schema["components"]["schemas"]["RunRequest"]["properties"]
        task_request_props = schema["components"]["schemas"]["TaskRequest"]["properties"]
        assert "current live owner" in task_request_props["state"]["description"]
        assert "concurrent participation" in task_request_props["cooperative"]["description"]
        assert "durable handoff history" in task_request_props["release_reason"]["description"]

        task_card = schema["components"]["schemas"]["TaskCard"]["properties"]
        assert task_card["state"]["enum"] == ["ready", "blocked", "deferred", "done"]
        assert "isolation_hint" in task_card
        assert "blocking_dependencies" in task_card
        assert "open dependencies is blocked" in task_card["operational_status"]["description"]
        assert "ordinary claimability" in task_card["blocking_dependencies"]["description"]
        for field in (
            "archived_at",
            "archive_note",
            "owner",
            "participants",
            "claims",
            "relations",
            "comments",
        ):
            assert field in task_card
        claim_view = schema["components"]["schemas"]["TaskClaimView"]["properties"]
        assert {
            "agent_name",
            "claimed_at",
            "claim_age_seconds",
            "claim_intent",
            "role",
        } <= set(claim_view)
        managed_ref = schema["components"]["schemas"]["ManagedTaskRef"]["properties"]
        assert {
            "claimed_at",
            "claim_age_seconds",
            "claim_intent",
            "role",
            "isolation_hint",
        } <= set(managed_ref)

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
        implicit_broadcast = client.post(
            "/actions/message",
            json={"agent_id": agent_id, "text": "ordinary broadcast"},
            headers=headers,
        )
        assert implicit_broadcast.status_code == 200

        omitted_alert = client.post(
            "/actions/message",
            json={"agent_id": agent_id, "text": "unsafe", "alert": True},
            headers=headers,
        )
        assert omitted_alert.status_code == 422
        assert "message.alert: ALERT requires an explicit destination" in omitted_alert.text

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
        command_only_read = client.post(
            "/actions/read", json={"cmd_hash": cmd_hash}, headers=headers
        )
        assert command_only_read.status_code == 200
        assert command_only_read.json()["status"] == "completed"

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
            "scope": "terminal:read",
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


def test_agent_facing_oauth_uses_one_read_scope():
    for path in (
        "/actions/run",
        "/actions/recovery",
        "/actions/cancel",
        "/actions/task",
        "/actions/context",
        "/actions/agent/start",
        "/actions/read",
        "/mcp",
    ):
        assert AuthMiddleware._scopes(path, "POST") == ["terminal:read"]


def test_refresh_rotation_is_single_use_under_concurrency(tmp_path):
    async def scenario():
        store = OAuthStore(tmp_path / "refresh-race.sqlite3")
        await store.initialize()
        client_id, _ = await store.register_client(
            ["https://client.example/callback"], "Concurrent client", "none"
        )
        token = await store.create_refresh(client_id, "terminal:read", 3600)
        results = await asyncio.gather(*(store.rotate_refresh(token) for _ in range(20)))
        return results

    results = asyncio.run(scenario())
    assert sum(result is not None for result in results) == 1

import asyncio
import hashlib
import sqlite3

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.auth.service import AuthService
from terminal_mcp.config import Settings


def settings(tmp_path, **updates):
    base = Settings(
        database_path=tmp_path / "db.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="test-secret-that-is-at-least-32-bytes",
        oauth_required_scopes="terminal:read",
        oauth_access_ttl_sec=900,
        oauth_refresh_ttl_sec=3600,
        mcp_auth_mode="oauth",
        actions_auth_mode="oauth",
    )
    return base.model_copy(update=updates)


def pairing_payload(secret):
    return {
        "secret": secret,
        "device_label": "Test phone",
        "public_key": "public-key-material-" + ("x" * 64),
    }


def test_pairing_exchange_registers_device_and_returns_usable_credentials(tmp_path, caplog):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        secret = asyncio.run(app.state.pairing_store.create())
        response = client.post("/pairing/exchange", json=pairing_payload(secret))

        assert response.status_code == 200
        body = response.json()
        assert body["device_id"].startswith("dev_")
        assert body["client_id"].startswith("device_")
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "terminal:read"
        assert body["refresh_token"]
        assert secret not in response.text
        assert secret not in caplog.text

        health = client.get(
            "/actions/health",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert health.status_code == 200

        with sqlite3.connect(app.state.settings.database_path) as db:
            device = db.execute(
                "SELECT client_id,public_key,label,created_at,last_used_at,revoked_at "
                "FROM console_devices WHERE device_id=?",
                (body["device_id"],),
            ).fetchone()
            client_row = db.execute(
                "SELECT auth_method,client_name FROM oauth_clients WHERE client_id=?",
                (body["client_id"],),
            ).fetchone()
            refresh_row = db.execute(
                "SELECT token_hash FROM oauth_refresh_tokens WHERE client_id=?",
                (body["client_id"],),
            ).fetchone()
            audit = db.execute(
                "SELECT event_type,outcome,reason,device_id "
                "FROM console_pairing_audit ORDER BY id"
            ).fetchall()

        assert device[0] == body["client_id"]
        assert device[1] == pairing_payload(secret)["public_key"]
        assert device[2] == "Test phone"
        assert device[3] == device[4]
        assert device[5] is None
        assert client_row == ("none", "Test phone")
        assert refresh_row == (hashlib.sha256(body["refresh_token"].encode()).hexdigest(),)
        assert audit[0][:3] == ("pairing_create", "success", "")
        assert audit[-1] == ("pairing_exchange", "success", "", body["device_id"])

        raw_db = app.state.settings.database_path.read_bytes()
        assert secret.encode() not in raw_db
        assert body["refresh_token"].encode() not in raw_db

        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": body["refresh_token"],
                "client_id": body["client_id"],
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"]
        assert refreshed.json()["refresh_token"] != body["refresh_token"]

        assert client.post("/pairing/create").status_code == 404
        schema = client.get("/openapi.json").json()
        assert "/pairing/exchange" not in schema["paths"]
        assert "/pairing/create" not in schema["paths"]
        assert "/connect" not in schema["paths"]



def test_bearer_actions_scope_paired_oauth_to_active_console_device(tmp_path):
    app = create_app(
        settings(
            tmp_path,
            actions_auth_mode="bearer",
            bearer_tokens="legacy-static-token",
            oauth_required_scopes="terminal:read terminal:execute",
            persistent_agents_enabled=True,
        )
    )
    with TestClient(app) as client:
        static_headers = {"Authorization": "Bearer legacy-static-token"}
        assert client.get("/actions/health", headers=static_headers).status_code == 200
        assert client.get("/actions/console/snapshot", headers=static_headers).status_code == 200

        secret = asyncio.run(app.state.pairing_store.create())
        paired = client.post("/pairing/exchange", json=pairing_payload(secret))
        assert paired.status_code == 200
        body = paired.json()
        paired_headers = {"Authorization": f"Bearer {body['access_token']}"}

        assert client.get("/actions/console/snapshot", headers=paired_headers).status_code == 200

        created = client.post(
            "/actions/persistent/slots/create",
            headers=paired_headers,
            json={"display_name": "Phone acceptance"},
        )
        assert created.status_code == 200
        created_body = created.json()
        assert created_body["ok"] is True
        logical_agent_id = created_body["slot"]["logical_agent_id"]
        revision = created_body["slot"]["slot_revision"]

        played = client.post(
            "/actions/persistent/slots/play",
            headers=paired_headers,
            json={
                "logical_agent_id": logical_agent_id,
                "expected_revision": revision,
                "idempotency_key": "paired-console-play",
            },
        )
        assert played.status_code == 200
        played_body = played.json()
        assert played_body["ok"] is True

        # Claim controls are on the Console allowlist too. Empty bodies fail
        # request validation (422), which proves auth passed without mutating state.
        for path in (
            "/actions/persistent/claims/release",
            "/actions/persistent/claims/reassign",
        ):
            assert client.post(path, headers=paired_headers, json={}).status_code == 422

        # Paired Console OAuth is intentionally not a general Actions credential:
        # command/session/task execution surfaces remain closed.
        for path, payload in (
            ("/actions/health", None),
            (
                "/actions/persistent/sessions/start",
                {
                    "selector": created_body["selector"]["selector"],
                    "expected_revision": played_body["slot"]["slot_revision"],
                },
            ),
            (
                "/actions/persistent/run",
                {
                    "logical_agent_id": logical_agent_id,
                    "work_session_id": "ws_fake",
                    "session_epoch": 1,
                    "cmd": "true",
                    "task_scope": "none",
                },
            ),
            (
                "/actions/persistent/task",
                {
                    "logical_agent_id": logical_agent_id,
                    "work_session_id": "ws_fake",
                    "session_epoch": 1,
                    "action": "comment",
                    "namespace": "ns",
                    "task_id": "T-1",
                    "payload": {},
                },
            ),
        ):
            response = (
                client.get(path, headers=paired_headers)
                if payload is None
                else client.post(path, headers=paired_headers, json=payload)
            )
            assert response.status_code == 401
            assert response.json() == {
                "error": "unauthorized",
                "detail": "invalid_token",
            }

        suspended = client.post(
            "/actions/persistent/slots/suspend",
            headers=paired_headers,
            json={
                "logical_agent_id": logical_agent_id,
                "expected_revision": played_body["slot"]["slot_revision"],
                "idempotency_key": "paired-console-suspend",
            },
        )
        assert suspended.status_code == 200
        suspended_body = suspended.json()
        assert suspended_body["ok"] is True

        rotated = client.post(
            "/actions/persistent/slots/rotate-selector",
            headers=paired_headers,
            json={
                "logical_agent_id": logical_agent_id,
                "selector": "A1B2",
                "expected_revision": suspended_body["slot"]["slot_revision"],
                "idempotency_key": "paired-console-rotate",
            },
        )
        assert rotated.status_code == 200
        rotated_body = rotated.json()
        assert rotated_body["ok"] is True

        deleted = client.post(
            "/actions/persistent/slots/delete",
            headers=paired_headers,
            json={
                "logical_agent_id": logical_agent_id,
                "expected_revision": rotated_body["slot"]["slot_revision"],
                "idempotency_key": "paired-console-delete",
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["ok"] is True

        ordinary_client_id, _ = asyncio.run(
            app.state.oauth_store.register_client(
                ["https://client.example/callback"], "Ordinary OAuth client", "none"
            )
        )
        ordinary_auth = AuthService(
            app.state.settings, app.state.oauth_store, app.state.credentials
        )
        ordinary_token = ordinary_auth.issue_access(
            ordinary_client_id, "terminal:read terminal:execute"
        )
        ordinary_headers = {"Authorization": f"Bearer {ordinary_token}"}

        for path, payload in (
            ("/actions/console/snapshot", None),
            ("/actions/persistent/slots/create", {"display_name": "Forbidden"}),
        ):
            response = (
                client.get(path, headers=ordinary_headers)
                if payload is None
                else client.post(path, headers=ordinary_headers, json=payload)
            )
            assert response.status_code == 401
            assert response.json() == {
                "error": "unauthorized",
                "detail": "invalid_token",
            }

        bogus = client.get(
            "/actions/console/snapshot",
            headers={"Authorization": "Bearer definitely-not-valid"},
        )
        assert bogus.status_code == 401
        assert bogus.json() == {"error": "unauthorized", "detail": "invalid_token"}

        assert asyncio.run(app.state.pairing_store.revoke_device(body["device_id"])) is True
        for path, payload in (
            ("/actions/console/snapshot", None),
            ("/actions/persistent/slots/create", {"display_name": "Revoked"}),
        ):
            response = (
                client.get(path, headers=paired_headers)
                if payload is None
                else client.post(path, headers=paired_headers, json=payload)
            )
            assert response.status_code == 401
            assert response.json() == {"error": "unauthorized", "detail": "invalid_token"}


def test_pairing_exchange_rejects_replay_expiry_and_malformed_without_secret_leak(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        secret = asyncio.run(app.state.pairing_store.create())
        assert client.post("/pairing/exchange", json=pairing_payload(secret)).status_code == 200

        replay = client.post("/pairing/exchange", json=pairing_payload(secret))
        assert replay.status_code == 400
        assert replay.json() == {"error": "invalid_pairing"}
        assert secret not in replay.text

        invalid_secret = "z" * 43
        invalid = client.post("/pairing/exchange", json=pairing_payload(invalid_secret))
        assert invalid.status_code == 400
        assert invalid.json() == {"error": "invalid_pairing"}
        assert invalid_secret not in invalid.text

        expired = asyncio.run(app.state.pairing_store.create(1, now=1))
        expired_response = client.post("/pairing/exchange", json=pairing_payload(expired))
        assert expired_response.status_code == 400
        assert expired_response.json() == {"error": "invalid_pairing"}
        assert expired not in expired_response.text

        malformed = client.post(
            "/pairing/exchange",
            json={"secret": "short", "device_label": "", "public_key": "x"},
        )
        assert malformed.status_code == 400
        assert malformed.json() == {"error": "invalid_request"}

        with sqlite3.connect(app.state.settings.database_path) as db:
            rejected = db.execute(
                "SELECT reason FROM console_pairing_audit "
                "WHERE event_type='pairing_exchange' AND outcome='rejected' ORDER BY id"
            ).fetchall()
        assert [row[0] for row in rejected] == ["replayed", "invalid", "expired", "malformed"]


def test_pairing_exchange_is_atomic_under_concurrency(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app):
        secret = asyncio.run(app.state.pairing_store.create())
        store = app.state.pairing_store

        async def exchange_once(label):
            return await store.exchange(
                secret,
                "public-key-" + ("x" * 64),
                label,
                "terminal:read",
                3600,
            )

        async def race():
            return await asyncio.gather(exchange_once("one"), exchange_once("two"))

        results = asyncio.run(race())
        successes = [result for result, _reason in results if result is not None]
        failures = [reason for result, reason in results if result is None]

        assert len(successes) == 1
        assert failures == ["replayed"]

        with sqlite3.connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT count(*) FROM console_devices").fetchone()[0] == 1
            assert db.execute(
                "SELECT count(*) FROM oauth_clients WHERE client_id LIKE 'device_%'"
            ).fetchone()[0] == 1


def test_device_revocation_invalidates_existing_access_and_refresh(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        secret = asyncio.run(app.state.pairing_store.create())
        body = client.post("/pairing/exchange", json=pairing_payload(secret)).json()
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        assert client.get("/actions/health", headers=headers).status_code == 200

        assert asyncio.run(app.state.pairing_store.revoke_device(body["device_id"])) is True
        assert client.get("/actions/health", headers=headers).status_code == 401
        assert asyncio.run(app.state.pairing_store.revoke_device(body["device_id"])) is False

        refresh = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": body["refresh_token"],
                "client_id": body["client_id"],
            },
        )
        assert refresh.status_code == 401
        assert refresh.json() == {"error": "invalid_client"}

        with sqlite3.connect(app.state.settings.database_path) as db:
            device = db.execute(
                "SELECT revoked_at FROM console_devices WHERE device_id=?",
                (body["device_id"],),
            ).fetchone()
            oauth_client = db.execute(
                "SELECT 1 FROM oauth_clients WHERE client_id=?", (body["client_id"],)
            ).fetchone()
            refresh_count = db.execute(
                "SELECT count(*) FROM oauth_refresh_tokens WHERE client_id=?",
                (body["client_id"],),
            ).fetchone()[0]
        assert device[0] is not None
        assert oauth_client is None
        assert refresh_count == 0


def test_connect_bootstrap_does_not_receive_or_render_fragment(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/connect")

    assert response.status_code == 200
    assert "pairing secret stays in the URL fragment" in response.text



def test_pairing_exchange_is_rate_limited(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        payload = pairing_payload("x" * 43)
        for _ in range(10):
            assert client.post("/pairing/exchange", json=payload).status_code == 400
        limited = client.post("/pairing/exchange", json=payload)

    assert limited.status_code == 429
    assert limited.json() == {"error": "rate_limited"}

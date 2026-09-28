import asyncio

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
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
        oauth_refresh_ttl_sec=3600,
        mcp_auth_mode="oauth",
        actions_auth_mode="oauth",
    )
    return base.model_copy(update=updates)


def payload(secret):
    return {"secret": secret, "device_label": "Browser", "public_key": "k" * 64}


def test_connect_bootstrap_security_headers_and_query_rejection(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        ok = client.get("/connect")
        bad = client.get("/connect?secret=must-not-reflect")
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"
    assert ok.headers["referrer-policy"] == "no-referrer"
    assert ok.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in ok.headers["content-security-policy"]
    assert '<meta name="referrer" content="no-referrer">' in ok.text
    assert bad.status_code == 400
    assert bad.json() == {"error": "invalid_connect_url"}
    assert "must-not-reflect" not in bad.text


def test_configured_origin_cors_and_disallowed_origin_does_not_consume_pairing(tmp_path):
    app = create_app(settings(tmp_path, console_allowed_origins="https://console.example"))
    with TestClient(app) as client:
        preflight = client.options(
            "/pairing/exchange",
            headers={
                "Origin": "https://console.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        secret = asyncio.run(app.state.pairing_store.create())
        rejected = client.post(
            "/pairing/exchange", headers={"Origin": "https://evil.example"}, json=payload(secret)
        )
        accepted = client.post(
            "/pairing/exchange",
            headers={"Origin": "https://console.example"},
            json=payload(secret),
        )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "https://console.example"
    assert rejected.status_code == 403
    assert rejected.json() == {"error": "origin_not_allowed"}
    assert accepted.status_code == 200
    assert accepted.headers["access-control-allow-origin"] == "https://console.example"
    assert accepted.headers["cache-control"] == "no-store"


def test_public_origin_is_allowed_and_bad_preflight_header_is_rejected(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        allowed = client.options(
            "/actions/health",
            headers={
                "Origin": "https://terminal.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        rejected = client.options(
            "/actions/health",
            headers={
                "Origin": "https://terminal.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-admin-token",
            },
        )
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "https://terminal.example"
    assert rejected.status_code == 403
    assert rejected.json() == {"error": "cors_preflight_rejected"}


def test_remote_pairing_creation_surface_does_not_exist(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        responses = [
            getattr(client, method)("/pairing/create")
            for method in ("get", "post", "put", "delete")
        ]
    assert all(response.status_code in {404, 405} for response in responses)


def test_wildcard_and_non_origin_configuration_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        create_app(settings(tmp_path, console_allowed_origins="*"))
    with pytest.raises(ValueError):
        create_app(settings(tmp_path, console_allowed_origins="https://console.example/path"))

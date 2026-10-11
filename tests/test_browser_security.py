import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.auth.identity_migration import (
    IdentityMigrationError,
    assert_identity_isolated,
)
from terminal_mcp.config import Settings


def settings(tmp_path, **updates):
    base = Settings(
        _env_file=None,
        _env_prefix=f"TMCP_OAUTH_TEST_{tmp_path.name.upper()}_",
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        env_file_path=tmp_path / "terminal-mcp.env",
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


def test_oauth_fixture_ignores_synthetic_external_auth_override(tmp_path, monkeypatch):
    """An inherited service path and dotenv never redirect a test to another auth store."""
    external_auth = tmp_path / "synthetic-external-auth.sqlite3"
    with closing(sqlite3.connect(external_auth)) as db, db:
        db.execute("CREATE TABLE oauth_identity_migration (invalid_marker TEXT)")
    external_snapshot = external_auth.stat()
    monkeypatch.setenv("TERMINAL_MCP_AUTH_DATABASE_PATH", str(external_auth))
    monkeypatch.setenv("TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS", "https://synthetic-env.example")
    prefix = f"TMCP_OAUTH_TEST_{tmp_path.name.upper()}_"
    (tmp_path / ".env").write_text(
        f"{prefix}CONSOLE_ALLOWED_ORIGINS=https://synthetic-dotenv.example\n"
    )
    with monkeypatch.context() as cwd_override:
        cwd_override.chdir(tmp_path)
        config = settings(tmp_path)

    auth = tmp_path / "auth.sqlite3"
    key = auth.with_name(auth.name + ".access-key")
    anchor = auth.with_name(auth.name + ".identity-anchor")
    blocked_paths = {
        external_auth,
        external_auth.with_name(external_auth.name + ".access-key"),
        external_auth.with_name(external_auth.name + ".identity-anchor"),
    }
    assert config.auth_database_path == auth
    assert config.database_path == tmp_path / "db.sqlite3"
    assert config.console_allowed_origins == ""

    # Treat even a stat/read attempt against the synthetic external store as a failure.
    original_stat, original_connect = Path.stat, sqlite3.connect

    def guarded_stat(path, *args, **kwargs):
        assert path not in blocked_paths, "fixture inspected inherited auth path"
        return original_stat(path, *args, **kwargs)

    def guarded_connect(database, *args, **kwargs):
        assert str(external_auth) not in str(database), "fixture opened inherited auth database"
        return original_connect(database, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "stat", guarded_stat)
        guard.setattr(sqlite3, "connect", guarded_connect)
        with TestClient(create_app(config)) as client:
            assert client.get("/connect").status_code == 200

    assert auth.is_file() and key.is_file() and anchor.is_file()
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert key.stat().st_mode & 0o777 == 0o600
    assert anchor.stat().st_mode & 0o777 == 0o600
    assert assert_identity_isolated(config.database_path, auth) is None
    assert external_auth.is_file()
    assert external_auth.stat().st_size == external_snapshot.st_size
    assert external_auth.stat().st_mtime_ns == external_snapshot.st_mtime_ns
    assert all(not path.exists() for path in blocked_paths - {external_auth})


def test_oauth_fixture_rejects_invalid_marker_without_resealing(tmp_path):
    """A test-only identity still fails closed on tampered lineage/version."""
    config = settings(tmp_path)
    with TestClient(create_app(config)) as client:
        assert client.get("/connect").status_code == 200

    auth = config.auth_database_path
    key = auth.with_name(auth.name + ".access-key")
    anchor = auth.with_name(auth.name + ".identity-anchor")
    key_inode, anchor_inode = key.stat().st_ino, anchor.stat().st_ino
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute("UPDATE oauth_identity_migration SET version=0")

    with pytest.raises(IdentityMigrationError, match="migration has not completed"):
        with TestClient(create_app(config)):
            pass

    with closing(sqlite3.connect(auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(0,)]
    assert key.stat().st_ino == key_inode
    assert anchor.stat().st_ino == anchor_inode

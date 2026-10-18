from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
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
        persistent_agents_enabled=True,
        access_mesh_enabled=True,
        fleet_instance_id="test-node",
        fleet_signing_private_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        fleet_peers_json="[]",
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
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
    return {"code": secret, "device_label": "Browser", "public_key": "k" * 64}


def test_connect_bootstrap_applies_security_headers(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        ok = client.get("/connect")
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"
    assert ok.headers["referrer-policy"] == "no-referrer"
    assert ok.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in ok.headers["content-security-policy"]
    assert '<meta name="referrer" content="no-referrer">' in ok.text


def test_public_origin_allows_authenticated_preflight(tmp_path):
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
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "https://terminal.example"

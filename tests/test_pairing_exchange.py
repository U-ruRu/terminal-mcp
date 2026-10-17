import asyncio
import hashlib
import sqlite3

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path, **updates):
    base = Settings(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal-mcp.env",
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

        with sqlite3.connect(app.state.settings.auth_database_path) as db:
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
                "SELECT event_type,outcome,reason,device_id FROM console_pairing_audit ORDER BY id"
            ).fetchall()

        assert device[0] == body["client_id"]
        assert device[1] == pairing_payload(secret)["public_key"]
        assert device[2] == "Test phone"
        assert device[3] == device[4]
        assert device[5] is None
        assert client_row == ("none", "Test phone")
        assert refresh_row == (hashlib.sha256(body["refresh_token"].encode()).hexdigest(),)
        assert audit[-1] == ("pairing_exchange", "success", "", body["device_id"])


async def test_pairing_exchange_race_commits_one_device(tmp_path):
    from terminal_mcp.auth.pairing import PairingStore
    from terminal_mcp.auth.storage import OAuthStore

    path = tmp_path / "auth.sqlite3"
    store = PairingStore(path)
    await OAuthStore(path).initialize()
    await store.initialize()
    secret = await store.create()

    results = await asyncio.gather(
        *[
            store.exchange(secret, "public-key-" + "x" * 64, label, "terminal:read", 3600)
            for label in ("one", "two")
        ]
    )

    assert sum(result is not None for result, _ in results) == 1
    assert [reason for result, reason in results if result is None] == ["replayed"]
    assert len(await store.list_devices()) == 1


async def test_device_revocation_removes_its_oauth_credentials(tmp_path):
    from terminal_mcp.auth.pairing import PairingStore
    from terminal_mcp.auth.storage import OAuthStore

    path = tmp_path / "auth.sqlite3"
    store = PairingStore(path)
    oauth = OAuthStore(path)
    await oauth.initialize()
    await store.initialize()
    secret = await store.create()
    paired, _ = await store.exchange(
        secret, "public-key-" + "x" * 64, "Phone", "terminal:read", 3600
    )

    assert await store.revoke_device(paired.device_id) is True

    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT revoked_at FROM console_devices WHERE device_id=?", (paired.device_id,)
            ).fetchone()[0]
            is not None
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM oauth_clients WHERE client_id=?", (paired.client_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM oauth_refresh_tokens WHERE client_id=?", (paired.client_id,)
            ).fetchone()[0]
            == 0
        )


def test_pairing_exchange_is_rate_limited(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        payload = pairing_payload("x" * 43)
        for _ in range(10):
            client.post("/pairing/exchange", json=payload)
        limited = client.post("/pairing/exchange", json=payload)

    assert limited.status_code == 429
    assert limited.json() == {"error": "rate_limited"}

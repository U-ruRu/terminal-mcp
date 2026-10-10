"""Isolation and migration regression tests. All credentials are synthetic."""

import asyncio
import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    IdentityMigrationError,
    assert_identity_isolated,
    clean_working_data,
    inspect,
    migrate,
)
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.config import Settings


def run(awaitable):
    return asyncio.run(awaitable)


def seed(tmp_path):
    runtime, auth = tmp_path / "runtime.sqlite3", tmp_path / "auth.sqlite3"
    run(AuthFoundationStore(auth).initialize())
    old = OAuthStore(runtime)
    pairs = PairingStore(runtime)
    run(old.initialize())
    run(pairs.initialize())
    cid, secret = run(
        old.register_client(["https://client.example/callback"], "chat-client", "none")
    )
    assert secret is None
    token = run(old.create_refresh(cid, "terminal:read", 30 * 86400))
    pairing_secret = run(pairs.create(now=int(time.time())))
    device, failure = run(
        pairs.exchange(
            pairing_secret,
            "public-key-only-" + "x" * 64,
            "android",
            "terminal:read",
            30 * 86400,
        )
    )
    assert failure == ""
    assert device is not None
    with sqlite3.connect(runtime) as db:
        expiry = db.execute(
            "SELECT expires_at FROM oauth_refresh_tokens WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()[0]
        devices = db.execute("SELECT device_id,client_id FROM console_devices").fetchall()
    return runtime, auth, cid, token, device, expiry, devices


def settings(tmp_path, runtime, auth):
    return Settings(
        database_path=runtime,
        auth_database_path=auth,
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal-mcp.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="synthetic-test-secret-is-at-least-32-bytes",
        oauth_required_scopes="terminal:read",
        oauth_access_ttl_sec=900,
        oauth_refresh_ttl_sec=30 * 86400,
        mcp_auth_mode="oauth",
        actions_auth_mode="oauth",
    )


def test_existing_chatgpt_and_android_survive_migrate_clean_restart(tmp_path):
    runtime, auth, cid, token, device, expiry, devices = seed(tmp_path)
    with pytest.raises(IdentityMigrationError, match="Legacy"):
        assert_identity_isolated(runtime, auth)
    app_settings = settings(tmp_path, runtime, auth)
    with pytest.raises(IdentityMigrationError, match="Legacy"):
        with TestClient(create_app(app_settings)):
            pass
    result = migrate(runtime, auth, tmp_path / "backup")
    assert result["status"] == "migrated"
    assert result["tables"] == 6
    assert inspect(runtime, auth)["status"] == "isolated"
    assert migrate(runtime, auth, tmp_path / "unused-backup")["status"] == "already-isolated"
    with sqlite3.connect(auth) as db:
        assert (
            db.execute(
                "SELECT expires_at FROM oauth_refresh_tokens WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()[0]
            == expiry
        )
        assert db.execute("SELECT device_id,client_id FROM console_devices").fetchall() == devices
        assert db.execute("SELECT count(*) FROM auth_principals").fetchone()[0] >= 0
    assert app_settings.oauth_refresh_ttl_sec == 30 * 86400
    assert_identity_isolated(runtime, auth)
    # Official clean start quarantines all runtime/output data and snapshots auth.
    output = tmp_path / "output.sqlite3"
    with sqlite3.connect(output) as cache:
        cache.execute("CREATE TABLE command_output(body TEXT)")
        cache.execute("INSERT INTO command_output VALUES('disposable')")
    result = clean_working_data(runtime, auth, output, tmp_path / "clean-quarantine")
    assert result["status"] == "cleaned"
    assert not runtime.exists() and not output.exists() and auth.exists()
    assert (tmp_path / "clean-quarantine" / "runtime.sqlite3").exists()
    assert (tmp_path / "clean-quarantine" / "output.sqlite3").exists()
    assert (tmp_path / "clean-quarantine" / "auth.before.sqlite3").exists()
    with TestClient(create_app(app_settings)) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": token},
        )
        assert response.status_code == 200, response.text
        rotated = response.json()["refresh_token"]
        assert rotated and rotated != token
        # A used refresh token is permanently invalid.
        assert client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": token},
        ).status_code in {400, 401}
        android_refresh = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": device.client_id,
                "refresh_token": device.refresh_token,
            },
        )
        assert android_refresh.status_code == 200, android_refresh.text
    # A second app process start retains grants; no new registration required.
    with TestClient(create_app(app_settings)) as client:
        assert (
            client.post(
                "/oauth/token",
                data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": rotated},
            ).status_code
            == 200
        )
    run(OAuthStore(auth).delete_client(cid))
    assert run(OAuthStore(auth).rotate_refresh(rotated)) is None
    assert run(PairingStore(auth).active_device_for_client(device.client_id)) is not None


def test_conflicting_destination_never_overwritten(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    # Valid existing OAuth material in the separate auth database means
    # operator intervention, not a merge with two independent sources.
    secure = OAuthStore(auth)
    run(secure.initialize())
    run(secure.register_client(["https://other.example/cb"], "existing", "none"))
    before = sqlite3.connect(auth).execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
    with pytest.raises(IdentityMigrationError, match="destination|conflict"):
        migrate(runtime, auth, tmp_path / "backups")
    after = sqlite3.connect(auth).execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
    assert after == before
    assert inspect(runtime, auth)["status"] == "legacy-present"


@pytest.mark.parametrize("phase", ["after_backup", "before_commit"])
def test_interrupted_migration_rolls_back_and_can_retry(tmp_path, phase):
    runtime, auth, cid, *_ = seed(tmp_path)
    backup = tmp_path / "backup1"
    with pytest.raises(IdentityMigrationError, match="injected"):
        migrate(runtime, auth, backup, fault=phase)
    with sqlite3.connect(runtime) as db:
        assert db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 2
    with sqlite3.connect(auth) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='oauth_clients'").fetchone()
            is None
        )
    assert (backup / "runtime.before.sqlite3").exists()
    assert (backup / "auth.before.sqlite3").exists()
    assert migrate(runtime, auth, tmp_path / "backup2")["status"] == "migrated"
    assert run(OAuthStore(auth).get_client(cid)) is not None


def test_reverse_uses_current_credentials_not_pre_cutover_backup(tmp_path):
    runtime, auth, cid, token, *_ = seed(tmp_path)
    assert migrate(runtime, auth, tmp_path / "forward-backup")["status"] == "migrated"
    live = OAuthStore(auth)
    new_token = run(live.create_refresh(cid, "terminal:read", 30 * 86400))
    assert run(live.rotate_refresh(token)) == (cid, "terminal:read")
    # Reverse after live auth changes MUST carry new credentials/revocations forward.
    assert migrate(runtime, auth, tmp_path / "reverse-backup", reverse=True)["status"] == "reversed"
    assert run(OAuthStore(runtime).rotate_refresh(new_token)) == (cid, "terminal:read")
    assert run(OAuthStore(runtime).rotate_refresh(token)) is None
    with sqlite3.connect(auth) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='auth_principals'").fetchone()
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='oauth_clients'").fetchone()
            is None
        )
    # A subsequently retried new release must require a new forward migration.
    with pytest.raises(IdentityMigrationError, match="Legacy"):
        assert_identity_isolated(runtime, auth)


def test_reverse_conflict_preserves_both_databases(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "backup")
    # An older binary could have recreated an empty legacy OAuth table.
    with sqlite3.connect(runtime) as db:
        db.execute("CREATE TABLE oauth_clients(client_id TEXT PRIMARY KEY)")
    with pytest.raises(IdentityMigrationError, match="conflict"):
        migrate(runtime, auth, tmp_path / "reverse-backup", reverse=True)
    assert inspect(runtime, auth)["auth_rows"] > 0


def test_expiration_and_device_revocation_after_migration(tmp_path):
    runtime, auth, cid, token, device, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "backup")
    now = int(time.time())
    with sqlite3.connect(auth) as db:
        db.execute(
            "UPDATE oauth_refresh_tokens SET expires_at=? WHERE token_hash=?",
            (now - 10, hashlib.sha256(token.encode()).hexdigest()),
        )
    assert run(OAuthStore(auth).rotate_refresh(token)) is None
    assert run(PairingStore(auth).revoke_device(device.device_id))
    assert run(OAuthStore(auth).rotate_refresh(device.refresh_token)) is None
    with sqlite3.connect(auth) as db:
        assert (
            db.execute(
                "SELECT revoked_at FROM console_devices WHERE device_id=?",
                (device.device_id,),
            ).fetchone()[0]
            is not None
        )


def test_same_physical_database_is_rejected(tmp_path):
    path = tmp_path / "one.sqlite3"
    sqlite3.connect(path).close()
    with pytest.raises(IdentityMigrationError, match="physically distinct"):
        assert_identity_isolated(path, path)
    alias = tmp_path / "hardlink.sqlite3"
    import os

    os.link(path, alias)
    with pytest.raises(IdentityMigrationError, match="physically distinct"):
        assert_identity_isolated(path, alias)


def test_clean_refuses_legacy_runtime_and_same_quarantine(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    output = tmp_path / "output.sqlite3"
    sqlite3.connect(output).close()
    with pytest.raises(IdentityMigrationError, match="Legacy"):
        clean_working_data(runtime, auth, output, tmp_path / "blocked-quarantine")
    migrate(runtime, auth, tmp_path / "backup")
    clean_working_data(runtime, auth, output, tmp_path / "approved-quarantine")
    with pytest.raises(IdentityMigrationError, match="already exists"):
        clean_working_data(runtime, auth, output, tmp_path / "approved-quarantine")


def test_sqlite_backup_restore_old_working_schema_and_current_auth_reverse(tmp_path):
    runtime, auth, cid, old_token, device, *_ = seed(tmp_path)
    backup = tmp_path / "forward-backup"
    assert migrate(runtime, auth, backup)["status"] == "migrated"
    with sqlite3.connect(backup / "runtime.before.sqlite3") as original:
        assert original.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert original.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 2
    with sqlite3.connect(backup / "auth.before.sqlite3") as original_auth:
        assert original_auth.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            original_auth.execute(
                "SELECT name FROM sqlite_master WHERE name='auth_principals'"
            ).fetchone()
            is not None
        )

    # Emulate old schema restore, stripped ONLY of stale identity tables in
    # the isolated COPY. Newest auth data must win during reverse.
    old_working = tmp_path / "rollback-working.sqlite3"
    with sqlite3.connect(backup / "runtime.before.sqlite3") as original:
        with sqlite3.connect(old_working) as restored:
            original.backup(restored)
    from terminal_mcp.auth.identity_migration import SCHEMAS

    with sqlite3.connect(old_working) as restored:
        for table in reversed(tuple(SCHEMAS)):
            restored.execute(f'DROP TABLE "{table}"')
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    latest = OAuthStore(auth)
    new_token = run(latest.create_refresh(cid, "terminal:read", 30 * 86400))
    assert run(latest.rotate_refresh(old_token)) == (cid, "terminal:read")
    assert (
        migrate(old_working, auth, tmp_path / "rollback-backup", reverse=True)["status"]
        == "reversed"
    )
    old_store = OAuthStore(old_working)
    assert run(old_store.rotate_refresh(old_token)) is None
    assert run(old_store.rotate_refresh(new_token)) == (cid, "terminal:read")
    assert run(PairingStore(old_working).active_device_for_client(device.client_id))

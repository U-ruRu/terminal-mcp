"""Isolation and migration regression tests. All credentials are synthetic."""

import asyncio
import hashlib
import shutil
import sqlite3
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    IdentityLineageError,
    IdentityMigrationError,
    assert_identity_isolated,
    clean_working_data,
    initialize_fresh_identity,
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
    with closing(sqlite3.connect(runtime)) as db, db:
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
    with closing(sqlite3.connect(auth)) as db, db:
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
    with closing(sqlite3.connect(output)) as cache, cache:
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
    with closing(sqlite3.connect(auth)) as db, db:
        before = db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
    with pytest.raises(IdentityMigrationError, match="destination|conflict"):
        migrate(runtime, auth, tmp_path / "backups")
    with closing(sqlite3.connect(auth)) as db, db:
        after = db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
    assert after == before
    assert inspect(runtime, auth)["status"] == "conflicting-identity-sources"


@pytest.mark.parametrize("phase", ["after_backup", "before_commit"])
def test_interrupted_migration_rolls_back_and_can_retry(tmp_path, phase):
    runtime, auth, cid, *_ = seed(tmp_path)
    backup = tmp_path / "backup1"
    with pytest.raises(IdentityMigrationError, match="injected"):
        migrate(runtime, auth, backup, fault=phase)
    with closing(sqlite3.connect(runtime)) as db, db:
        assert db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 2
    with closing(sqlite3.connect(auth)) as db, db:
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
    with closing(sqlite3.connect(auth)) as db, db:
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
    with closing(sqlite3.connect(runtime)) as db, db:
        db.execute("CREATE TABLE oauth_clients(client_id TEXT PRIMARY KEY)")
    with pytest.raises(IdentityMigrationError, match="conflict"):
        migrate(runtime, auth, tmp_path / "reverse-backup", reverse=True)
    assert inspect(runtime, auth)["auth_rows"] > 0


def test_expiration_and_device_revocation_after_migration(tmp_path):
    runtime, auth, cid, token, device, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "backup")
    now = int(time.time())
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(
            "UPDATE oauth_refresh_tokens SET expires_at=? WHERE token_hash=?",
            (now - 10, hashlib.sha256(token.encode()).hexdigest()),
        )
    assert run(OAuthStore(auth).rotate_refresh(token)) is None
    assert run(PairingStore(auth).revoke_device(device.device_id))
    assert run(OAuthStore(auth).rotate_refresh(device.refresh_token)) is None
    with closing(sqlite3.connect(auth)) as db, db:
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
    with closing(sqlite3.connect(backup / "runtime.before.sqlite3")) as original, original:
        assert original.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert original.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= 2
    with closing(sqlite3.connect(backup / "auth.before.sqlite3")) as original_auth, original_auth:
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
    with closing(sqlite3.connect(backup / "runtime.before.sqlite3")) as original, original:
        with closing(sqlite3.connect(old_working)) as restored, restored:
            original.backup(restored)
    from terminal_mcp.auth.identity_migration import SCHEMAS

    with closing(sqlite3.connect(old_working)) as restored, restored:
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


# Fail-closed cases B1–B4 independently reported by Zulu-2 QA.


def test_b1_missing_protected_auth_fails_before_any_source_change(tmp_path):
    runtime, auth, cid, *_ = seed(tmp_path)
    auth.unlink()
    assert not auth.exists()
    with pytest.raises(IdentityMigrationError, match="protected auth database is missing"):
        migrate(runtime, auth, tmp_path / "forbidden-backup")
    assert not auth.exists()  # Never silently recreate AuthFoundation.
    assert not (tmp_path / "forbidden-backup").exists()
    assert run(OAuthStore(runtime).get_client(cid)) is not None


def test_b2_destination_without_unique_client_id_fails_closed(tmp_path):
    runtime, auth, cid, *_ = seed(tmp_path)
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(
            "CREATE TABLE console_devices("
            "device_id TEXT PRIMARY KEY,client_id TEXT NOT NULL,"
            "public_key TEXT NOT NULL,label TEXT NOT NULL,"
            "created_at INTEGER NOT NULL,last_used_at INTEGER NOT NULL,revoked_at INTEGER)"
        )
        db.execute("CREATE INDEX ix_console_devices_client ON console_devices(client_id)")
    with pytest.raises(IdentityMigrationError, match="DDL conflict"):
        migrate(runtime, auth, tmp_path / "forbidden-backup")
    assert run(OAuthStore(runtime).get_client(cid)) is not None
    with closing(sqlite3.connect(auth)) as db:
        assert db.execute("SELECT count(*) FROM console_devices").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", ["empty", "foundation-only", "missing-marker"])
def test_b3_replacement_auth_blocks_startup_and_clean(tmp_path, replacement):
    runtime, auth, cid, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "migration-backup")
    auth.unlink()
    if replacement == "empty":
        sqlite3.connect(auth).close()
    else:
        run(AuthFoundationStore(auth).initialize())
        if replacement == "missing-marker":
            run(OAuthStore(auth).initialize())
            run(PairingStore(auth).initialize())
    assert inspect(runtime, auth)["status"] == "incompatible-protected-auth"
    with pytest.raises(IdentityMigrationError, match="protected|migration"):
        assert_identity_isolated(runtime, auth)
    output = tmp_path / "output.sqlite3"
    with closing(sqlite3.connect(output)) as db, db:
        db.execute("CREATE TABLE output_sentinel(value TEXT)")
        db.execute("INSERT INTO output_sentinel VALUES('present')")
    with pytest.raises(IdentityMigrationError, match="protected|migration"):
        clean_working_data(runtime, auth, output, tmp_path / "blocked-clean")
    assert runtime.exists() and output.exists()
    with pytest.raises(IdentityMigrationError, match="protected|migration"):
        with TestClient(create_app(settings(tmp_path, runtime, auth))):
            pass
    assert not (tmp_path / "blocked-clean").exists()


def test_b3_loss_of_auth_after_clean_is_not_mistaken_for_first_install(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "migration-backup")
    output = tmp_path / "output.sqlite3"
    sqlite3.connect(output).close()
    clean_working_data(runtime, auth, output, tmp_path / "clean-backup")
    assert not runtime.exists() and auth.exists()
    auth.unlink()  # Access key remains and proves previous installation.
    with pytest.raises(IdentityMigrationError, match="protected auth database"):
        assert_identity_isolated(runtime, auth)


def test_b4_forward_preserves_deleted_tail_audit_autoincrement(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    with closing(sqlite3.connect(runtime)) as db, db:
        issued = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('synthetic','success',?)",
            (int(time.time()),),
        ).lastrowid
        db.execute("DELETE FROM console_pairing_audit WHERE id=?", (issued,))
        source_seq = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
        ).fetchone()[0]
        assert source_seq >= issued
        assert (
            db.execute("SELECT COALESCE(MAX(id),0) FROM console_pairing_audit").fetchone()[0]
            < source_seq
        )
    migrate(runtime, auth, tmp_path / "forward-backup")
    with closing(sqlite3.connect(auth)) as db, db:
        assert (
            db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
            ).fetchone()[0]
            == source_seq
        )
        new_id = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('later','success',?)",
            (int(time.time()),),
        ).lastrowid
        assert new_id > source_seq


def test_b4_reverse_preserves_deleted_tail_audit_autoincrement(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "forward-backup")
    with closing(sqlite3.connect(auth)) as db, db:
        issued = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('post-cutover','success',?)",
            (int(time.time()),),
        ).lastrowid
        db.execute("DELETE FROM console_pairing_audit WHERE id=?", (issued,))
        source_seq = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
        ).fetchone()[0]
        assert source_seq >= issued
    migrate(runtime, auth, tmp_path / "reverse-backup", reverse=True)
    with closing(sqlite3.connect(runtime)) as db, db:
        assert (
            db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
            ).fetchone()[0]
            == source_seq
        )
        new_id = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('after-rollback','success',?)",
            (int(time.time()),),
        ).lastrowid
        assert new_id > issued


def test_fresh_install_seals_auth_marker_and_survives_restart(tmp_path):
    runtime = tmp_path / "first-runtime.sqlite3"
    auth = tmp_path / "first-auth.sqlite3"
    app_settings = settings(tmp_path, runtime, auth)
    assert not runtime.exists() and not auth.exists()
    assert assert_identity_isolated(runtime, auth) is True
    with TestClient(create_app(app_settings)) as client:
        assert client.get("/health/live").status_code == 200
    with closing(sqlite3.connect(auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
    assert assert_identity_isolated(runtime, auth) is None
    with TestClient(create_app(app_settings)) as client:
        assert client.get("/health/live").status_code == 200


def test_foundation_schema_version_drift_blocks_forward_even_with_valid_oauth(tmp_path):
    runtime, auth, cid, *_ = seed(tmp_path)
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute("PRAGMA user_version=321")
    with pytest.raises(IdentityMigrationError, match="AuthFoundation schema version"):
        migrate(runtime, auth, tmp_path / "uncreated-backup")
    assert not (tmp_path / "uncreated-backup").exists()
    assert run(OAuthStore(runtime).get_client(cid)) is not None


def test_b4_deleted_all_audit_rows_keeps_highwater(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    with closing(sqlite3.connect(runtime)) as db, db:
        db.execute("DELETE FROM console_pairing_audit")
        highwater = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
        ).fetchone()[0]
        assert highwater > 0
        assert db.execute("SELECT count(*) FROM console_pairing_audit").fetchone()[0] == 0
    migrate(runtime, auth, tmp_path / "backup")
    with closing(sqlite3.connect(auth)) as db, db:
        new_id = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('later','success',?)",
            (int(time.time()),),
        ).lastrowid
        assert new_id > highwater


def test_b2_missing_explicit_target_index_fails_closed(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(
            "CREATE TABLE console_devices("
            "device_id TEXT PRIMARY KEY,client_id TEXT NOT NULL UNIQUE,"
            "public_key TEXT NOT NULL,label TEXT NOT NULL,"
            "created_at INTEGER NOT NULL,last_used_at INTEGER NOT NULL,revoked_at INTEGER)"
        )
    with pytest.raises(IdentityMigrationError, match="index DDL conflict"):
        migrate(runtime, auth, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_inspect_reports_protected_auth_loss_after_clean(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "forward-backup")
    output = tmp_path / "output.sqlite3"
    sqlite3.connect(output).close()
    clean_working_data(runtime, auth, output, tmp_path / "clean-quarantine")
    auth.unlink()
    assert inspect(runtime, auth)["status"] == "missing-protected-auth"
    with pytest.raises(IdentityMigrationError, match="protected auth database"):
        assert_identity_isolated(runtime, auth)


def test_b2_extra_unique_index_in_empty_target_is_refused(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(
            "CREATE TABLE console_devices("
            "device_id TEXT PRIMARY KEY,client_id TEXT NOT NULL UNIQUE,"
            "public_key TEXT NOT NULL,label TEXT NOT NULL,"
            "created_at INTEGER NOT NULL,last_used_at INTEGER NOT NULL,revoked_at INTEGER)"
        )
        db.execute("CREATE INDEX ix_console_devices_client ON console_devices(client_id)")
        db.execute("CREATE UNIQUE INDEX unexpected_label_unique ON console_devices(label)")
    with pytest.raises(IdentityMigrationError, match="unexpected identity indexes"):
        migrate(runtime, auth, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_first_install_rejects_deleted_access_mesh_history_with_missing_auth(tmp_path):
    runtime = tmp_path / "runtime.sqlite3"
    auth = tmp_path / "auth.sqlite3"
    with closing(sqlite3.connect(runtime)) as db, db:
        db.execute(
            "CREATE TABLE access_mesh_history(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
        )
        issued = db.execute("INSERT INTO access_mesh_history(name) VALUES('used')").lastrowid
        db.execute("DELETE FROM access_mesh_history WHERE id=?", (issued,))
    with pytest.raises(IdentityMigrationError, match="protected auth database"):
        assert_identity_isolated(runtime, auth)


def test_b3_foreign_complete_auth_cannot_replace_existing_installation(tmp_path):
    """Reproduce Delta-3's confirmed B3 with full unrelated Foundation/marker."""
    old = tmp_path / "existing"
    other = tmp_path / "other-installation"
    old.mkdir()
    other.mkdir()
    runtime, auth, cid, refresh, device, *_ = seed(old)
    original = AuthFoundationStore(auth)
    principal = run(original.create_principal("legacy-person", "synthetic-password"))
    member = run(original.create_client(principal["principal_id"], "agent", "known-agent"))
    run(
        original.create_grant(
            principal["principal_id"],
            member["client_id"],
            ["https://synthetic.example"],
            ["terminal:read"],
            role="qa",
        )
    )
    run(original.bootstrap_realm("old-realm", "old-node", "old-signing", "old-public-key"))
    assert migrate(runtime, auth, old / "forward-backup")["status"] == "migrated"

    key_file = auth.with_name(auth.name + ".access-key")
    key_digest = hashlib.sha256(key_file.read_bytes()).hexdigest()
    # Trusted *current* auth checkpoint. Only this protected store matches
    # the installation key; restoring this copy requires no reauthorization.
    checkpoint = old / "trusted-current-auth.sqlite3"
    with closing(sqlite3.connect(auth)) as current:
        with closing(sqlite3.connect(checkpoint)) as backup:
            current.backup(backup)

    foreign_main, foreign_auth = other / "runtime.sqlite3", other / "auth.sqlite3"
    with TestClient(create_app(settings(other, foreign_main, foreign_auth))) as foreign_client:
        assert foreign_client.get("/health/live").status_code == 200
    with closing(sqlite3.connect(foreign_auth)) as other_db:
        assert other_db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
        assert other_db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] == 0

    # Replace only the auth database, keeping original Access verifier key.
    shutil.copy2(foreign_auth, auth)
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() == key_digest
    assert inspect(runtime, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        assert_identity_isolated(runtime, auth)
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        with TestClient(create_app(settings(old, runtime, auth))):
            pass
    output = old / "output.sqlite3"
    with closing(sqlite3.connect(output)) as db, db:
        db.execute("CREATE TABLE protected_working_sentinel(value TEXT)")
        db.execute("INSERT INTO protected_working_sentinel VALUES('still here')")
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        clean_working_data(runtime, auth, output, old / "unsafe-clean")
    assert runtime.is_file() and output.is_file()
    assert not (old / "unsafe-clean").exists()

    # A matching online backup restores every previously registered identity.
    shutil.copy2(checkpoint, auth)
    assert inspect(runtime, auth)["status"] == "isolated"
    assert assert_identity_isolated(runtime, auth) is None
    assert (
        run(AuthFoundationStore(auth).verify_password("legacy-person", "synthetic-password"))
        is not None
    )
    assert run(OAuthStore(auth).get_client(cid)) is not None
    assert run(PairingStore(auth).active_device_for_client(device.client_id))
    with TestClient(create_app(settings(old, runtime, auth))) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": refresh},
        )
        assert response.status_code == 200, response.text
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() == key_digest


def test_b3_tampered_lineage_nonce_is_refused_without_clean(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "forward-backup")
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(
            "UPDATE oauth_identity_migration SET lineage_nonce=?",
            ("ab" * 32,),
        )
    assert inspect(runtime, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        assert_identity_isolated(runtime, auth)
    output = tmp_path / "output.sqlite3"
    sqlite3.connect(output).close()
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        clean_working_data(runtime, auth, output, tmp_path / "blocked-clean")
    assert runtime.exists() and output.exists()


def test_b3_legacy_main_missing_auth_is_distinct_from_readiness(tmp_path):
    runtime, auth, cid, *_ = seed(tmp_path)
    auth.unlink()
    state = inspect(runtime, auth)
    assert state["status"] == "missing-protected-auth"
    assert state["runtime_tables"] == 6
    assert run(OAuthStore(runtime).get_client(cid)) is not None
    with pytest.raises(IdentityMigrationError, match="protected auth database is missing"):
        migrate(runtime, auth, tmp_path / "must-not-backup")
    assert not (tmp_path / "must-not-backup").exists()


def test_inspect_legacy_runtime_with_foreign_completed_auth_cannot_claim_ready(tmp_path):
    old = tmp_path / "old"
    foreign = tmp_path / "foreign"
    old.mkdir()
    foreign.mkdir()
    main, auth, cid, *_ = seed(old)
    with TestClient(
        create_app(settings(foreign, foreign / "runtime.sqlite3", foreign / "auth.sqlite3"))
    ):
        pass
    shutil.copy2(foreign / "auth.sqlite3", auth)
    result = inspect(main, auth)
    assert result["status"] == "untrusted-protected-auth"
    assert result["runtime_tables"] == 6
    assert run(OAuthStore(main).get_client(cid)) is not None
    with pytest.raises(IdentityMigrationError):
        migrate(main, auth, old / "unsafe-forward")
    assert not (old / "unsafe-forward").exists()


def test_b3_foreign_authority_after_legitimate_clean_remains_untrusted(tmp_path):
    original, foreign = tmp_path / "original", tmp_path / "foreign"
    original.mkdir()
    foreign.mkdir()
    main, auth, cid, *_ = seed(original)
    migrate(main, auth, original / "migration-backup")
    clean_working_data(main, auth, original / "output.sqlite3", original / "quarantine")
    assert not main.exists()
    with TestClient(
        create_app(settings(foreign, foreign / "main.sqlite3", foreign / "auth.sqlite3"))
    ):
        pass
    shutil.copy2(foreign / "auth.sqlite3", auth)
    assert inspect(main, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError, match="lineage mismatch"):
        assert_identity_isolated(main, auth)
    assert (original / "quarantine" / "runtime.sqlite3").exists()


def test_b3_swapped_verifier_key_is_detected_and_original_key_recovers(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "different"
    old.mkdir()
    new.mkdir()
    runtime, auth, cid, *_ = seed(old)
    migrate(runtime, auth, old / "backup")
    verifier = auth.with_name(auth.name + ".access-key")
    key_copy = old / "known-original-verifier-key.fixture"
    shutil.copy2(verifier, key_copy)
    with TestClient(create_app(settings(new, new / "main.sqlite3", new / "auth.sqlite3"))):
        pass
    shutil.copy2((new / "auth.sqlite3").with_name("auth.sqlite3.access-key"), verifier)
    assert inspect(runtime, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError):
        assert_identity_isolated(runtime, auth)
    shutil.copy2(key_copy, verifier)
    assert inspect(runtime, auth)["status"] == "isolated"
    assert run(OAuthStore(auth).get_client(cid)) is not None


def test_b3_foreign_complete_auth_using_same_verifier_key_rejected(tmp_path):
    """Strongest B3: structural marker and HMAC use same key, but wrong instance."""
    old, foreign = tmp_path / "existing", tmp_path / "foreign"
    old.mkdir()
    foreign.mkdir()
    runtime, auth, cid, token, device, *_ = seed(old)
    migrate(runtime, auth, old / "forward-backup")
    original_anchor = auth.with_name(auth.name + ".identity-anchor").read_bytes()
    old_key = auth.with_name(auth.name + ".access-key")
    key_before = hashlib.sha256(old_key.read_bytes()).hexdigest()
    other_main, other_auth = foreign / "main.sqlite3", foreign / "auth.sqlite3"
    shutil.copy2(old_key, other_auth.with_name(other_auth.name + ".access-key"))
    run(AuthFoundationStore(other_auth).initialize())
    run(OAuthStore(other_auth).initialize())
    run(PairingStore(other_auth).initialize())
    initialize_fresh_identity(other_main, other_auth)
    with closing(sqlite3.connect(other_auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
        assert db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] == 0
    assert (
        other_auth.with_name(other_auth.name + ".identity-anchor").read_bytes() != original_anchor
    )

    shutil.copy2(other_auth, auth)  # NOT the original external installation anchor.
    assert old_key.read_bytes() and hashlib.sha256(old_key.read_bytes()).hexdigest() == key_before
    assert auth.with_name(auth.name + ".identity-anchor").read_bytes() == original_anchor
    assert inspect(runtime, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError, match="external anchor differs"):
        assert_identity_isolated(runtime, auth)
    with pytest.raises(IdentityLineageError):
        clean_working_data(runtime, auth, old / "output.sqlite3", old / "forbidden-clean")
    assert runtime.exists() and not (old / "forbidden-clean").exists()


def test_b3_missing_external_installation_anchor_blocks_everything(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "backup")
    witness = auth.with_name(auth.name + ".identity-anchor")
    assert witness.stat().st_mode & 0o777 == 0o600
    witness.unlink()
    assert inspect(runtime, auth)["status"] == "untrusted-protected-auth"
    with pytest.raises(IdentityLineageError, match="anchor is missing"):
        assert_identity_isolated(runtime, auth)
    with pytest.raises(IdentityLineageError):
        clean_working_data(runtime, auth, tmp_path / "output.sqlite3", tmp_path / "blocked-clean")
    assert runtime.exists()


def test_b3_clean_backups_preserve_independent_anchor(tmp_path):
    runtime, auth, *_ = seed(tmp_path)
    migrate(runtime, auth, tmp_path / "forward")
    witness = auth.with_name(auth.name + ".identity-anchor")
    assert witness.is_file()
    clean_working_data(runtime, auth, tmp_path / "output.sqlite3", tmp_path / "clean-quarantine")
    captured = tmp_path / "clean-quarantine" / "auth.identity-anchor.before"
    assert captured.read_bytes() == witness.read_bytes()
    assert captured.stat().st_mode & 0o777 == 0o600

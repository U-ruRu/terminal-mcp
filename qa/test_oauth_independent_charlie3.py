"""Independent QA replay on 6849760 pinned lineage candidate (Delta-3 fixtures, updated statuses).
All records, credentials, and paths are synthetic and confined to pytest tmp_path.
"""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_identity_migration import seed, settings

from terminal_mcp.app import create_app
from terminal_mcp.auth.foundation import AuthFoundationStore
from terminal_mcp.auth.identity_migration import (
    INDEXES,
    SCHEMAS,
    IdentityMigrationError,
    assert_identity_isolated,
    clean_working_data,
    inspect,
    migrate,
)
from terminal_mcp.auth.storage import OAuthStore


def run(coro):
    return asyncio.run(coro)


def digest_tables(path: Path, names):
    """No credential value is printed; hash exact SQLite tuples per relation."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        result = {}
        for name in names:
            tuples = db.execute(f'SELECT * FROM "{name}"').fetchall()
            result[name] = (
                len(tuples),
                hashlib.sha256(repr(sorted(tuples, key=repr)).encode()).hexdigest(),
            )
        return result


def make_index(conn, table):
    for name, (owner, columns) in INDEXES.items():
        if owner == table:
            conn.execute(f'CREATE INDEX "{name}" ON "{owner}"({columns})')


def audit_sequence(path):
    with closing(sqlite3.connect(path)) as db:
        row = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='console_pairing_audit'"
        ).fetchone()
        return row[0] if row else None


@pytest.mark.parametrize(
    "fault",
    [
        "missing_auth",
        "empty_auth",
        "version_321",
        "missing_verifier",
        "missing_security",
        "missing_principals_table",
    ],
)
def test_b1_invalid_foundation_never_changes_legacy_identity(tmp_path, fault):
    main, auth, *_ = seed(tmp_path)
    before = digest_tables(main, SCHEMAS)
    if fault == "missing_auth":
        auth.unlink()
    elif fault == "empty_auth":
        auth.unlink()
        with closing(sqlite3.connect(auth)):
            pass
    elif fault == "version_321":
        with sqlite3.connect(auth) as db:
            db.execute("PRAGMA user_version=321")
    elif fault == "missing_verifier":
        Path(str(auth) + ".access-key").unlink()
    elif fault == "missing_security":
        with sqlite3.connect(auth) as db:
            db.execute("DELETE FROM auth_security_state")
    else:
        with sqlite3.connect(auth) as db:
            db.execute("DROP TABLE auth_principals")
    backup = tmp_path / "must-not-appear"
    with pytest.raises(IdentityMigrationError):
        migrate(main, auth, backup)
    assert not backup.exists()
    assert digest_tables(main, SCHEMAS) == before
    if fault == "missing_auth":
        assert not auth.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "missing_unique",
        "missing_explicit_index",
        "extra_unique",
        "default_drift",
        "audit_without_autoincrement",
    ],
)
def test_b2_invalid_empty_target_rejected_without_mutation(tmp_path, fault):
    main, auth, *_ = seed(tmp_path)
    original_main = digest_tables(main, SCHEMAS)
    if fault in {"missing_unique", "extra_unique"}:
        table = "console_devices"
    elif fault in {"missing_explicit_index", "default_drift"}:
        table = "oauth_codes"
    else:
        table = "console_pairing_audit"
    ddl = SCHEMAS[table]
    if fault == "missing_unique":
        ddl = ddl.replace("client_id TEXT NOT NULL UNIQUE", "client_id TEXT NOT NULL")
    elif fault == "default_drift":
        ddl = ddl.replace("used INTEGER NOT NULL DEFAULT 0", "used INTEGER NOT NULL DEFAULT 1")
    elif fault == "audit_without_autoincrement":
        ddl = ddl.replace("AUTOINCREMENT", "")
    with sqlite3.connect(auth) as db:
        db.execute(f'CREATE TABLE "{table}" {ddl}')
        if fault != "missing_explicit_index":
            make_index(db, table)
        if fault == "extra_unique":
            db.execute("CREATE UNIQUE INDEX qa_unexpected ON console_devices(public_key)")
    before_auth = digest_tables(auth, [table])
    backup = tmp_path / "must-not-appear"
    with pytest.raises(IdentityMigrationError):
        migrate(main, auth, backup)
    assert not backup.exists()
    assert digest_tables(main, SCHEMAS) == original_main
    assert digest_tables(auth, [table]) == before_auth


def test_b2_compatible_empty_target_is_valid(tmp_path):
    main, auth, *_ = seed(tmp_path)
    original = digest_tables(main, SCHEMAS)
    with closing(sqlite3.connect(auth)) as db, db:
        db.execute(f'CREATE TABLE "console_devices" {SCHEMAS["console_devices"]}')
        make_index(db, "console_devices")
    assert migrate(main, auth, tmp_path / "forward")["status"] == "migrated"
    assert digest_tables(auth, SCHEMAS) == original
    with closing(sqlite3.connect(auth)) as db:
        assert any(row[3] == "u" for row in db.execute("PRAGMA index_list('console_devices')"))
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "empty",
        "missing_marker",
        "missing_oauth_table",
        "missing_verifier",
        "wrong_foundation_version",
    ],
)
def test_b3_bad_protected_auth_blocks_inspect_startup_and_clean(tmp_path, fault):
    main, auth, *_ = seed(tmp_path)
    assert migrate(main, auth, tmp_path / "forward")["status"] == "migrated"
    output = tmp_path / "output.sqlite3"
    with sqlite3.connect(output) as db:
        db.execute("CREATE TABLE retained(v TEXT)")
        db.execute("INSERT INTO retained VALUES('synthetic-keep')")
    original_main = main.read_bytes()
    original_output = output.read_bytes()
    if fault == "missing":
        auth.unlink()
    elif fault == "empty":
        auth.unlink()
        with closing(sqlite3.connect(auth)):
            pass
    elif fault == "missing_marker":
        with sqlite3.connect(auth) as db:
            db.execute("DROP TABLE oauth_identity_migration")
    elif fault == "missing_oauth_table":
        with sqlite3.connect(auth) as db:
            db.execute("DROP TABLE console_devices")
    elif fault == "missing_verifier":
        Path(str(auth) + ".access-key").unlink()
    else:
        with sqlite3.connect(auth) as db:
            db.execute("PRAGMA user_version=321")
    assert inspect(main, auth)["status"] in {
        "missing-protected-auth",
        "incompatible-protected-auth",
        "untrusted-protected-auth",
    }
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)
    with pytest.raises(IdentityMigrationError):
        with TestClient(create_app(settings(tmp_path, main, auth))):
            pass
    quarantine = tmp_path / "must-not-quarantine"
    with pytest.raises(IdentityMigrationError):
        clean_working_data(main, auth, output, quarantine)
    assert not quarantine.exists()
    assert main.read_bytes() == original_main
    assert output.read_bytes() == original_output


def test_b3_clean_then_auth_loss_stays_fail_closed(tmp_path):
    main, auth, *_ = seed(tmp_path)
    migrate(main, auth, tmp_path / "forward")
    clean_working_data(main, auth, tmp_path / "output.sqlite3", tmp_path / "clean")
    auth.unlink()  # Access key remains as historical identity evidence.
    assert not main.exists()
    assert inspect(main, auth)["status"] in {
        "missing-protected-auth",
        "incompatible-protected-auth",
        "untrusted-protected-auth",
    }
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)


def test_b3_legitimate_first_install_seals_marker_and_restarts(tmp_path):
    main, auth = tmp_path / "main.sqlite3", tmp_path / "auth.sqlite3"
    assert inspect(main, auth)["status"] == "new-install"
    assert assert_identity_isolated(main, auth) is True
    with TestClient(create_app(settings(tmp_path, main, auth))) as client:
        assert client.get("/health/live").status_code == 200
    assert inspect(main, auth)["status"] == "isolated"
    with closing(sqlite3.connect(auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    with TestClient(create_app(settings(tmp_path, main, auth))) as client:
        assert client.get("/health/live").status_code == 200


def test_b3_stale_access_mesh_history_not_first_install(tmp_path):
    main, auth = tmp_path / "main.sqlite3", tmp_path / "auth.sqlite3"
    with sqlite3.connect(main) as db:
        db.execute("CREATE TABLE access_mesh_test(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        db.execute("INSERT INTO access_mesh_test DEFAULT VALUES")
        db.execute("DELETE FROM access_mesh_test")
    assert inspect(main, auth)["status"] in {
        "missing-protected-auth",
        "incompatible-protected-auth",
        "untrusted-protected-auth",
    }
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)


def test_b4_deleted_everything_and_reverse_keeps_high_water(tmp_path):
    main, auth, cid, *_ = seed(tmp_path)
    with sqlite3.connect(main) as db:
        db.execute("DELETE FROM console_pairing_audit")
    prior_seq = audit_sequence(main)
    assert prior_seq is not None and prior_seq > 0
    assert migrate(main, auth, tmp_path / "forward")["status"] == "migrated"
    assert audit_sequence(auth) == prior_seq
    with sqlite3.connect(auth) as db:
        issued = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('synthetic','ok',12345)"
        ).lastrowid
        assert issued > prior_seq
        db.execute("DELETE FROM console_pairing_audit WHERE id=?", (issued,))
    assert audit_sequence(auth) == issued
    assert migrate(main, auth, tmp_path / "reverse", reverse=True)["status"] == "reversed"
    assert audit_sequence(main) == issued
    with sqlite3.connect(main) as db:
        next_id = db.execute(
            "INSERT INTO console_pairing_audit(event_type,outcome,created_at) "
            "VALUES('synthetic','ok',12346)"
        ).lastrowid
        assert next_id > issued
    assert run(OAuthStore(main).get_client(cid)) is not None


@pytest.mark.parametrize("fault", ["after_backup", "before_commit"])
def test_offline_interruption_retry_keeps_legacy_then_recovers(tmp_path, fault):
    main, auth, *_ = seed(tmp_path)
    original = digest_tables(main, SCHEMAS)
    with pytest.raises(IdentityMigrationError):
        migrate(main, auth, tmp_path / "interrupted", fault=fault)
    assert digest_tables(main, SCHEMAS) == original
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)
    assert migrate(main, auth, tmp_path / "retry")["status"] == "migrated"
    assert digest_tables(auth, SCHEMAS) == original
    assert inspect(main, auth)["status"] == "isolated"


def test_all_oauth_and_foundation_relations_survive_clean_and_rollback(tmp_path):
    main, auth, cid, old_token, device, expiry, _ = seed(tmp_path)
    f = AuthFoundationStore(auth)
    p = run(f.create_principal("delta3-qa-user", "synthetic-only-testing-password"))
    c = run(f.create_client(p["principal_id"], "agent", "delta3-qa-client"))
    grant = run(
        f.create_grant(
            p["principal_id"],
            c["client_id"],
            ["https://qa.example/resource"],
            ["terminal:read"],
            role="qa",
        )
    )
    run(f.bootstrap_realm("delta3-realm", "delta3-node", "qa-signing-id", "synthetic-key"))
    with closing(sqlite3.connect(auth)) as db:
        foundation = [
            x[0]
            for x in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'auth_%' ORDER BY name"
            )
        ]
    original_foundation = digest_tables(auth, foundation)
    original_oauth = digest_tables(main, SCHEMAS)
    verifier_file = Path(str(auth) + ".access-key")
    original_key_hash = hashlib.sha256(verifier_file.read_bytes()).hexdigest()
    assert migrate(main, auth, tmp_path / "forward")["status"] == "migrated"
    assert migrate(main, auth, tmp_path / "idempotent")["status"] == "already-isolated"
    assert not (tmp_path / "idempotent").exists()
    assert digest_tables(auth, SCHEMAS) == original_oauth
    assert digest_tables(auth, foundation) == original_foundation
    assert hashlib.sha256(verifier_file.read_bytes()).hexdigest() == original_key_hash
    assert (
        clean_working_data(main, auth, tmp_path / "output.sqlite3", tmp_path / "quarantine")[
            "status"
        ]
        == "cleaned"
    )
    assert digest_tables(auth, SCHEMAS) == original_oauth
    assert digest_tables(auth, foundation) == original_foundation
    assert run(f.verify_password("delta3-qa-user", "synthetic-only-testing-password")) is not None
    assert any(g["grant_id"] == grant["grant_id"] for g in run(f.active_grants(p["principal_id"])))
    fresh_token = run(OAuthStore(auth).create_refresh(cid, "terminal:read", 30 * 86400))
    assert run(OAuthStore(auth).rotate_refresh(old_token)) == (cid, "terminal:read")
    # Reverse into the empty, compatible runtime copy created by the rollback procedure.
    with closing(sqlite3.connect(main)):
        pass
    assert migrate(main, auth, tmp_path / "reverse", reverse=True)["status"] == "reversed"
    assert run(OAuthStore(main).rotate_refresh(old_token)) is None
    assert run(OAuthStore(main).rotate_refresh(fresh_token)) == (cid, "terminal:read")
    assert digest_tables(auth, foundation) == original_foundation
    assert hashlib.sha256(verifier_file.read_bytes()).hexdigest() == original_key_hash
    assert expiry is not None and device is not None


def test_wal_backup_valid_v3_preserves_source_and_permissions(tmp_path):
    main, auth, cid, *_ = seed(tmp_path)
    with closing(sqlite3.connect(main)) as m:
        assert m.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    with closing(sqlite3.connect(auth)) as a:
        assert a.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    original = digest_tables(main, SCHEMAS)
    assert migrate(main, auth, tmp_path / "snapshots")["status"] == "migrated"
    assert digest_tables(auth, SCHEMAS) == original
    backup_main = tmp_path / "snapshots" / "runtime.before.sqlite3"
    backup_auth = tmp_path / "snapshots" / "auth.before.sqlite3"
    assert backup_main.exists() and backup_auth.exists()
    assert digest_tables(backup_main, SCHEMAS) == original
    with closing(sqlite3.connect(backup_auth)) as b:
        assert b.execute("PRAGMA user_version").fetchone()[0] == 3
        assert b.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert (backup_main.stat().st_mode & 0o777) == 0o600
    assert (backup_auth.stat().st_mode & 0o777) == 0o600
    assert (tmp_path / "snapshots").stat().st_mode & 0o777 == 0o700
    assert run(OAuthStore(auth).get_client(cid)) is not None


def test_b3_other_instance_valid_empty_authority_must_not_replace_existing_users(tmp_path):
    import shutil

    old_root = tmp_path / "original"
    old_root.mkdir()
    main, auth, cid, *_ = seed(old_root)
    f = AuthFoundationStore(auth)
    run(f.create_principal("preexisting-qa-principal", "synthetic-only-password"))
    original_key = Path(str(auth) + ".access-key").read_bytes()
    assert migrate(main, auth, old_root / "forward")["status"] == "migrated"
    assert run(OAuthStore(auth).get_client(cid)) is not None
    fresh_root = tmp_path / "different-fresh-instance"
    fresh_root.mkdir()
    fresh_main = fresh_root / "main.sqlite3"
    fresh_auth = fresh_root / "auth.sqlite3"
    with TestClient(create_app(settings(fresh_root, fresh_main, fresh_auth))) as client:
        assert client.get("/health/live").status_code == 200
    with closing(sqlite3.connect(fresh_auth)) as db:
        assert db.execute("SELECT version FROM oauth_identity_migration").fetchall() == [(1,)]
        assert db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 0
    shutil.copy2(fresh_auth, auth)
    assert Path(str(auth) + ".access-key").read_bytes() == original_key
    assert run(OAuthStore(auth).get_client(cid)) is None
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)
    assert inspect(main, auth)["status"] in {
        "missing-protected-auth",
        "incompatible-protected-auth",
        "untrusted-protected-auth",
    }
    with pytest.raises(IdentityMigrationError):
        clean_working_data(
            main, auth, old_root / "output.sqlite3", old_root / "clean-should-refuse"
        )
    assert main.exists()


def test_b3_inspect_precutover_auth_missing_reports_invalid(tmp_path):
    main, auth, *_ = seed(tmp_path)
    auth.unlink()
    assert inspect(main, auth)["status"] in {
        "missing-protected-auth",
        "incompatible-protected-auth",
        "untrusted-protected-auth",
    }


def test_original_z11_positive_wal_uses_supported_foundation_version(tmp_path):
    # Legacy z11 had set both databases to version 321. Only the protected
    # AuthFoundation (auth) must be version 3; the runtime DB version is disposable.
    main, auth, *_ = seed(tmp_path)
    for path, version in ((main, 321), (auth, 3)):
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
            db.execute(f"PRAGMA user_version={version}")
            db.commit()
    previous = digest_tables(main, SCHEMAS)
    assert migrate(main, auth, tmp_path / "backup")["status"] == "migrated"
    assert digest_tables(auth, SCHEMAS) == previous
    assert digest_tables(tmp_path / "backup" / "runtime.before.sqlite3", SCHEMAS) == previous
    with closing(sqlite3.connect(tmp_path / "backup" / "auth.before.sqlite3")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert inspect(main, auth)["status"] == "isolated"

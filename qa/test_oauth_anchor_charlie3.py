"""New independent TMCP-03 OAuth lineage acceptance probes; disposable synthetic fixtures only."""

import hashlib
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_identity_migration import run, seed, settings

from terminal_mcp.app import create_app
from terminal_mcp.auth.identity_migration import (
    IdentityMigrationError,
    assert_identity_isolated,
    clean_working_data,
    inspect,
    migrate,
)
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.storage import OAuthStore
from terminal_mcp.backup import (
    BackupValidationError,
    authoritative_backup_manifest,
    create_authoritative_snapshot,
    validate_authoritative_snapshot,
)


def path_for(auth: Path, suffix: str) -> Path:
    return Path(str(auth) + suffix)


def setup_paired(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    main, auth, cid, token, device, expiry, devices = seed(root)
    assert migrate(main, auth, root / "migration-snapshot")["status"] == "migrated"
    assert inspect(main, auth)["status"] == "isolated"
    return main, auth, cid, token, device, expiry, devices


def table_digest(db: Path, table: str) -> str:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        values = con.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    return hashlib.sha256(repr(values).encode()).hexdigest()


def verify_original_auth(auth: Path, cid: str, token: str, device):
    assert run(OAuthStore(auth).get_client(cid)) is not None
    assert run(OAuthStore(auth).rotate_refresh(token)) is not None
    assert run(PairingStore(auth).active_device_for_client(device.client_id)) is not None


@pytest.mark.parametrize(
    "variant",
    [
        "foreign-same-key",
        "foreign-other-key",
        "wrong-anchor",
        "wrong-key",
        "missing-anchor",
        "corrupt-anchor",
        "insecure-anchor",
        "marker-proof-drift",
    ],
)
def test_provenance_denies_tampering_without_clean_or_boot(variant, tmp_path):
    original = tmp_path / "original"
    main, auth, cid, token, device, expiry, _ = setup_paired(original)
    stable_auth = tmp_path / "auth-trusted.sqlite3"
    stable_key = tmp_path / "access-key-trusted"
    stable_anchor = tmp_path / "anchor-trusted"
    shutil.copy2(auth, stable_auth)
    shutil.copy2(path_for(auth, ".access-key"), stable_key)
    shutil.copy2(path_for(auth, ".identity-anchor"), stable_anchor)
    output = original / "output.sqlite3"
    with sqlite3.connect(output) as con:
        con.execute("CREATE TABLE persistent(v INTEGER)")
        con.execute("INSERT INTO persistent VALUES(7)")
    main_before = main.read_bytes()
    output_before = output.read_bytes()
    digest_before = table_digest(auth, "oauth_clients")

    if variant.startswith("foreign-") or variant in {"wrong-anchor", "wrong-key"}:
        secondary = tmp_path / "unrelated"
        secondary.mkdir()
        othermain, otherauth, *_ = seed(secondary)
        if variant == "foreign-same-key":
            shutil.copy2(stable_key, path_for(otherauth, ".access-key"))
        assert migrate(othermain, otherauth, secondary / "forward")["status"] == "migrated"
        if variant.startswith("foreign-"):
            if variant == "foreign-same-key":
                assert path_for(otherauth, ".access-key").read_bytes() == stable_key.read_bytes()
            shutil.copy2(otherauth, auth)
        elif variant == "wrong-anchor":
            shutil.copy2(
                path_for(otherauth, ".identity-anchor"), path_for(auth, ".identity-anchor")
            )
        else:
            shutil.copy2(path_for(otherauth, ".access-key"), path_for(auth, ".access-key"))
    elif variant == "missing-anchor":
        path_for(auth, ".identity-anchor").unlink()
    elif variant == "corrupt-anchor":
        path_for(auth, ".identity-anchor").write_bytes(b"partial-synthetic-anchor")
    elif variant == "insecure-anchor":
        path_for(auth, ".identity-anchor").chmod(0o644)
    else:
        with sqlite3.connect(auth) as con:
            con.execute("UPDATE oauth_identity_migration SET lineage_proof=?", ("0" * 64,))

    assert inspect(main, auth)["status"] in {
        "untrusted-protected-auth",
        "incompatible-protected-auth",
    }
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)
    with pytest.raises(IdentityMigrationError):
        with TestClient(create_app(settings(original, main, auth))):
            pass
    quarantine = original / "quarantine-should-not-exist"
    with pytest.raises(IdentityMigrationError):
        clean_working_data(main, auth, output, quarantine)
    assert not quarantine.exists()
    assert main.read_bytes() == main_before
    assert output.read_bytes() == output_before

    # Same-installation recovery: restore original DB, key and independent anchor.
    shutil.copy2(stable_auth, auth)
    shutil.copy2(stable_key, path_for(auth, ".access-key"))
    shutil.copy2(stable_anchor, path_for(auth, ".identity-anchor"))
    assert inspect(main, auth)["status"] == "isolated"
    assert table_digest(auth, "oauth_clients") == digest_before
    verify_original_auth(auth, cid, token, device)


def test_real_sigkill_after_anchor_write_retry_keeps_nonce_and_oauth(tmp_path):
    main, auth, cid, token, device, _, _ = seed(tmp_path)
    source_before = {
        t: table_digest(main, t)
        for t in (
            "oauth_clients",
            "oauth_refresh_tokens",
            "console_devices",
            "console_pairing_audit",
        )
    }
    subcode = """
import os,signal,sys
from pathlib import Path
import terminal_mcp.auth.identity_migration as m
original = m._new_lineage
def cut(*a, **kw):
    proof = original(*a, **kw)
    os.kill(os.getpid(), signal.SIGKILL)
    return proof
m._new_lineage = cut
m.migrate(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]))
"""
    proc = subprocess.run(
        [sys.executable, "-c", subcode, str(main), str(auth), str(tmp_path / "first-try")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stderr[-300:])
    anchor = path_for(auth, ".identity-anchor")
    assert anchor.is_file() and anchor.stat().st_mode & 0o777 == 0o600
    nonce_witness = anchor.read_bytes()
    with sqlite3.connect(auth) as con:
        assert (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='oauth_identity_migration'"
            ).fetchone()
            is None
        )
    assert inspect(main, auth)["status"] == "legacy-present"
    assert {t: table_digest(main, t) for t in source_before} == source_before
    with pytest.raises(IdentityMigrationError):
        assert_identity_isolated(main, auth)
    assert migrate(main, auth, tmp_path / "second-try")["status"] == "migrated"
    assert anchor.read_bytes() == nonce_witness
    assert inspect(main, auth)["status"] == "isolated"
    verify_original_auth(auth, cid, token, device)


def test_corrupt_pending_anchor_refuses_offline_retry(tmp_path):
    main, auth, *_ = seed(tmp_path)
    source = table_digest(main, "oauth_clients")
    path_for(auth, ".identity-anchor").write_bytes(b"broken-pending-anchor")
    path_for(auth, ".identity-anchor").chmod(0o600)
    with pytest.raises(IdentityMigrationError):
        migrate(main, auth, tmp_path / "backup")
    assert path_for(auth, ".identity-anchor").read_bytes() == b"broken-pending-anchor"
    assert table_digest(main, "oauth_clients") == source
    with sqlite3.connect(auth) as con:
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='oauth_identity_migration'"
        ).fetchall()


def test_empty_first_install_with_stray_anchor_not_reborn(tmp_path):
    main, auth = tmp_path / "runtime.sqlite3", tmp_path / "auth.sqlite3"
    anchor = path_for(auth, ".identity-anchor")
    anchor.write_bytes(b"synthetic-uncommitted")
    anchor.chmod(0o600)
    assert inspect(main, auth)["status"] == "missing-protected-auth"
    with pytest.raises(IdentityMigrationError):
        with TestClient(create_app(settings(tmp_path, main, auth))):
            pass
    assert anchor.read_bytes() == b"synthetic-uncommitted"
    assert not auth.exists()


def test_backup_inventory_verified_snapshot_and_paired_recovery(tmp_path):
    node = tmp_path / "node"
    main, auth, cid, token, device, _, _ = setup_paired(node)
    cfg = settings(node, main, auth).model_copy(
        update={
            "fleet_v1_source_enabled": False,
            "fleet_v1_authority_enabled": False,
            "fleet_v1_projection_enabled": False,
            "fleet_v1_public_enabled": False,
            "fleet_legacy_replication_enabled": False,
        }
    )
    cfg.env_file_path.write_text("TMCP_QA_SYNTHETIC=1\n")
    cfg.env_file_path.chmod(0o600)
    manifest = authoritative_backup_manifest(cfg)
    member = {x["member_id"]: x for x in manifest["members"]}
    assert member["oauth_installation_anchor"]["required"] is True
    assert member["oauth_installation_anchor"]["secret"] is True
    snapshot = tmp_path / "snapshot"
    with pytest.raises(BackupValidationError):
        create_authoritative_snapshot(cfg, snapshot, quiesced=False)
    receipt = create_authoritative_snapshot(cfg, snapshot, quiesced=True)
    assert receipt["quiesced"] is True
    assert validate_authoritative_snapshot(snapshot)["quiesced"] is True
    snapdata = json.loads((snapshot / "manifest.json").read_text())
    files = {m["member_id"]: snapshot / m["snapshot_file"] for m in snapdata["members"]}
    assert {"auth_database", "access_verifier_key", "oauth_installation_anchor"} <= files.keys()
    restored = tmp_path / "restored"
    restored.mkdir()
    recovered_main, recovered_auth = restored / "runtime.sqlite3", restored / "auth.sqlite3"
    shutil.copy2(files["auth_database"], recovered_auth)
    shutil.copy2(files["access_verifier_key"], path_for(recovered_auth, ".access-key"))
    shutil.copy2(files["oauth_installation_anchor"], path_for(recovered_auth, ".identity-anchor"))
    assert inspect(recovered_main, recovered_auth)["status"] == "isolated"
    verify_original_auth(recovered_auth, cid, token, device)
    missing = path_for(recovered_auth, ".identity-anchor")
    missing.unlink()
    assert inspect(recovered_main, recovered_auth)["status"] == "untrusted-protected-auth"
    shutil.copy2(files["oauth_installation_anchor"], missing)
    assert inspect(recovered_main, recovered_auth)["status"] == "isolated"
    # Damaged snapshot is not valid for automated recovery.
    with files["oauth_installation_anchor"].open("ab") as fp:
        fp.write(b"tampered")
    with pytest.raises(BackupValidationError):
        validate_authoritative_snapshot(snapshot)


def test_clean_restart_preserves_pairings_expiry_and_anchor(tmp_path):
    main, auth, cid, token, device, expiry, devices = setup_paired(tmp_path)
    anchor = path_for(auth, ".identity-anchor")
    witness = anchor.read_bytes()
    before = {
        t: table_digest(auth, t)
        for t in (
            "oauth_clients",
            "oauth_refresh_tokens",
            "console_pairings",
            "console_devices",
            "console_pairing_audit",
        )
    }
    output = tmp_path / "output.sqlite3"
    with sqlite3.connect(output) as con:
        con.execute("CREATE TABLE disposable(i INTEGER)")
    q = tmp_path / "clean"
    assert clean_working_data(main, auth, output, q)["status"] == "cleaned"
    assert not main.exists() and not output.exists()
    assert anchor.read_bytes() == witness and anchor.stat().st_mode & 0o777 == 0o600
    assert (q / "auth.identity-anchor.before").read_bytes() == witness
    assert {t: table_digest(auth, t) for t in before} == before
    with sqlite3.connect(auth) as con:
        assert (
            con.execute(
                "SELECT expires_at FROM oauth_refresh_tokens WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()[0]
            == expiry
        )
    with TestClient(create_app(settings(tmp_path, main, auth))) as client:
        response = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": token},
        )
        assert response.status_code == 200
        rotated = response.json()["refresh_token"]
        assert rotated != token
        assert client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": token},
        ).status_code in {400, 401}
    with TestClient(create_app(settings(tmp_path, main, auth))) as client:
        assert (
            client.post(
                "/oauth/token",
                data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": rotated},
            ).status_code
            == 200
        )
    assert run(PairingStore(auth).active_device_for_client(device.client_id)) is not None
    assert inspect(main, auth)["status"] == "isolated"


def test_clean_rolls_back_mid_rename_failure(tmp_path, monkeypatch):
    main, auth, cid, token, device, _, _ = setup_paired(tmp_path)
    output = tmp_path / "out.sqlite3"
    with sqlite3.connect(output) as conn:
        conn.execute("CREATE TABLE retained(v INTEGER)")
        conn.execute("INSERT INTO retained VALUES(42)")
    main_before, out_before = main.read_bytes(), output.read_bytes()
    original_rename = Path.rename

    def interrupted_rename(path, destination):
        if Path(path) == output:
            raise OSError("synthetic-mid-clean-output-rename-failure")
        return original_rename(path, destination)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "rename", interrupted_rename)
        with pytest.raises(OSError, match="synthetic-mid-clean"):
            clean_working_data(main, auth, output, tmp_path / "failed-clean")
    assert main.exists() and output.exists()
    assert main.read_bytes() == main_before
    assert output.read_bytes() == out_before
    assert inspect(main, auth)["status"] == "isolated"
    assert (
        clean_working_data(main, auth, output, tmp_path / "successful-clean")["status"] == "cleaned"
    )
    verify_original_auth(auth, cid, token, device)


def test_clean_anchor_backup_failure_preserves_source(tmp_path, monkeypatch):
    from terminal_mcp.auth import identity_migration as im

    main, auth, cid, token, device, _, _ = setup_paired(tmp_path)
    output = tmp_path / "cache.sqlite3"
    with sqlite3.connect(output) as conn:
        conn.execute("CREATE TABLE retained(v INTEGER)")
    main_before = main.read_bytes()
    anchor = path_for(auth, ".identity-anchor")
    anchor_before = anchor.read_bytes()
    with monkeypatch.context() as patcher:

        def simulated_failure(*args, **kwargs):
            raise OSError("synthetic-anchor-backup-interruption")

        patcher.setattr(im, "_backup_anchor", simulated_failure)
        with pytest.raises(OSError, match="synthetic-anchor-backup"):
            clean_working_data(main, auth, output, tmp_path / "failed-quarantine")
    assert main.read_bytes() == main_before
    assert output.exists() and anchor.read_bytes() == anchor_before
    assert inspect(main, auth)["status"] == "isolated"
    assert (
        clean_working_data(main, auth, output, tmp_path / "retry-quarantine")["status"] == "cleaned"
    )
    verify_original_auth(auth, cid, token, device)


def test_missing_anchor_remains_required_by_backup_inventory(tmp_path):
    from terminal_mcp.backup import validate_authoritative_backup_source

    main, auth, *_ = setup_paired(tmp_path)
    cfg = settings(tmp_path, main, auth).model_copy(
        update={
            "fleet_v1_source_enabled": False,
            "fleet_v1_authority_enabled": False,
            "fleet_v1_projection_enabled": False,
            "fleet_v1_public_enabled": False,
        }
    )
    cfg.env_file_path.write_text("TMCP_QA_SYNTHETIC=1\n")
    cfg.env_file_path.chmod(0o600)
    anchor = path_for(auth, ".identity-anchor")
    held = tmp_path / "known-original-anchor"
    shutil.copy2(anchor, held)
    anchor.unlink()
    manifest = authoritative_backup_manifest(cfg)
    entries = {m["member_id"]: m for m in manifest["members"]}
    assert entries["oauth_installation_anchor"]["required"] is True
    with pytest.raises(BackupValidationError, match="oauth_installation_anchor"):
        validate_authoritative_backup_source(cfg)
    shutil.copy2(held, anchor)
    assert inspect(main, auth)["status"] == "isolated"
    validated = validate_authoritative_backup_source(cfg)
    assert "oauth_installation_anchor" in {item["member_id"] for item in validated["validated"]}

import os
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.auth.access_key import AccessVerifierKeyStore
from terminal_mcp.backup import (
    BackupValidationError,
    access_verifier_key_path,
    validate_authoritative_backup_source,
)
from terminal_mcp.config import Settings
from terminal_mcp.storage.sqlite import SCHEMA_VERSION


def _sqlite(path: Path, version: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.execute("CREATE TABLE IF NOT EXISTS marker(id INTEGER PRIMARY KEY)")
        db.commit()
    path.chmod(0o600)


def _settings(tmp_path: Path) -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = tmp_path / "terminal-mcp.env"
    env.write_text("TERMINAL_MCP_AUTH_MODE=none\n")
    env.chmod(0o600)
    return Settings(
        env_file_path=env,
        database_path=tmp_path / "terminal-mcp.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        fleet_legacy_replication_enabled=False,
        fleet_instance_id="",
        fleet_signing_private_key="",
        fleet_peers_json="[]",
        fleet_node_meta_path=tmp_path / "fleet-node-meta.sqlite3",
        fleet_control_path=tmp_path / "fleet-control.sqlite3",
        fleet_projection_path=tmp_path / "fleet-projection.sqlite3",
    )


def _complete_source(settings: Settings, *, fleet=False):
    _sqlite(settings.database_path, SCHEMA_VERSION)
    _sqlite(settings.auth_database_path, 3)
    AccessVerifierKeyStore(access_verifier_key_path(settings.auth_database_path)).load_or_create(
        allow_create=True
    )
    if fleet:
        _sqlite(settings.effective_fleet_node_meta_path(), 1)
        _sqlite(settings.effective_fleet_control_path(), 3)


def test_validation_accepts_complete_standalone_state_and_optional_runtime_config(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)

    result = validate_authoritative_backup_source(settings)
    rows = {item["member_id"]: item for item in result["validated"]}

    assert rows["runtime_database"]["user_version"] == SCHEMA_VERSION
    assert rows["auth_database"]["user_version"] == 3
    assert rows["access_verifier_key"]["mode"] == "0600"
    assert "runtime_config" not in rows


def test_validation_requires_fleet_pair_when_fleet_state_exists(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    _sqlite(settings.effective_fleet_control_path(), 3)

    with pytest.raises(BackupValidationError, match="fleet_node_meta"):
        validate_authoritative_backup_source(settings)


def test_validation_rejects_insecure_access_key_permissions(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    key = access_verifier_key_path(settings.auth_database_path)
    key.chmod(0o644)

    with pytest.raises(BackupValidationError, match="insecure permissions"):
        validate_authoritative_backup_source(settings)


def test_validation_rejects_corrupt_sqlite_member(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    settings.database_path.write_bytes(os.urandom(128))
    settings.database_path.chmod(0o600)

    with pytest.raises(BackupValidationError, match="sqlite validation failed"):
        validate_authoritative_backup_source(settings)


def test_snapshot_copies_authoritative_runtime_rows(tmp_path):
    from terminal_mcp.backup import create_authoritative_snapshot

    settings = _settings(tmp_path / "source")
    _complete_source(settings)
    with sqlite3.connect(settings.database_path) as db:
        db.execute("INSERT INTO marker VALUES(73)")

    destination = tmp_path / "snapshot"
    create_authoritative_snapshot(settings, destination, quiesced=True)

    with sqlite3.connect(destination / "files" / "runtime_database.sqlite3") as db:
        assert db.execute("SELECT id FROM marker").fetchall() == [(73,)]


def test_snapshot_validation_detects_tampered_member(tmp_path):
    from terminal_mcp.backup import (
        create_authoritative_snapshot,
        validate_authoritative_snapshot,
    )

    settings = _settings(tmp_path / "source")
    _complete_source(settings)
    destination = tmp_path / "snapshot"
    create_authoritative_snapshot(settings, destination, quiesced=True)

    runtime_copy = destination / "files" / "runtime_database.sqlite3"
    runtime_copy.write_bytes(runtime_copy.read_bytes() + b"tamper")

    with pytest.raises(BackupValidationError, match="size mismatch|checksum mismatch"):
        validate_authoritative_snapshot(destination)


def test_snapshot_failure_removes_partial_directory(tmp_path, monkeypatch):
    import terminal_mcp.backup as backup_module

    settings = _settings(tmp_path / "source")
    _complete_source(settings)
    destination = tmp_path / "snapshot"

    original = backup_module._copy_sqlite_snapshot
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BackupValidationError("injected snapshot failure")
        return original(source, target)

    monkeypatch.setattr(backup_module, "_copy_sqlite_snapshot", fail_second)

    with pytest.raises(BackupValidationError, match="injected"):
        backup_module.create_authoritative_snapshot(settings, destination, quiesced=True)

    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))


@pytest.mark.asyncio
async def test_snapshot_preserves_the_current_installation_anchor(tmp_path):
    from terminal_mcp.auth.foundation import AuthFoundationStore
    from terminal_mcp.auth.identity_migration import initialize_fresh_identity
    from terminal_mcp.auth.pairing import PairingStore
    from terminal_mcp.auth.storage import OAuthStore
    from terminal_mcp.backup import create_authoritative_snapshot

    settings = _settings(tmp_path / "source")
    await AuthFoundationStore(settings.auth_database_path).initialize()
    await OAuthStore(settings.auth_database_path).initialize()
    await PairingStore(settings.auth_database_path).initialize()
    initialize_fresh_identity(settings.database_path, settings.auth_database_path)
    _sqlite(settings.database_path, SCHEMA_VERSION)
    original = settings.auth_database_path.with_name(
        settings.auth_database_path.name + ".identity-anchor"
    )
    expected = original.read_bytes()
    destination = tmp_path / "snapshot"

    result = create_authoritative_snapshot(settings, destination, quiesced=True)

    member = next(
        item for item in result["validated"] if item["member_id"] == "oauth_installation_anchor"
    )
    assert (destination / member["snapshot_file"]).read_bytes() == expected

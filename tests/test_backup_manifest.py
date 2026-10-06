import os
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.auth.access_key import AccessVerifierKeyStore
from terminal_mcp.backup import (
    BackupValidationError,
    access_verifier_key_path,
    authoritative_backup_manifest,
    validate_authoritative_backup_source,
)
from terminal_mcp.config import Settings


def _sqlite(path: Path, version: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.execute("CREATE TABLE IF NOT EXISTS marker(id INTEGER PRIMARY KEY)")
        db.commit()
    path.chmod(0o600)


def _settings(tmp_path: Path) -> Settings:
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
    _sqlite(settings.database_path, 19)
    _sqlite(settings.auth_database_path, 3)
    AccessVerifierKeyStore(access_verifier_key_path(settings.auth_database_path)).load_or_create(
        allow_create=True
    )
    if fleet:
        _sqlite(settings.effective_fleet_node_meta_path(), 1)
        _sqlite(settings.effective_fleet_control_path(), 3)


def test_manifest_names_authoritative_state_and_excludes_derived_cache(tmp_path):
    settings = _settings(tmp_path)
    _sqlite(settings.effective_fleet_node_meta_path())
    _sqlite(settings.effective_fleet_control_path())
    manifest = authoritative_backup_manifest(settings)
    members = {item["member_id"]: item for item in manifest["members"]}
    excluded = {item["member_id"] for item in manifest["excluded"]}

    assert manifest["manifest_version"] == 1
    assert {
        "node_environment",
        "runtime_database",
        "auth_database",
        "access_verifier_key",
        "fleet_node_meta",
        "fleet_control",
        "runtime_config",
    } == set(members)
    assert members["fleet_node_meta"]["required"] is True
    assert members["fleet_control"]["required"] is True
    assert members["access_verifier_key"]["secret"] is True
    assert {"output_cache", "fleet_projection", "log"} == excluded


def test_fleet_members_are_optional_on_clean_standalone_node(tmp_path):
    settings = _settings(tmp_path)
    manifest = authoritative_backup_manifest(settings)
    members = {item["member_id"]: item for item in manifest["members"]}

    assert members["fleet_node_meta"]["required"] is False
    assert members["fleet_control"]["required"] is False


def test_existing_fleet_state_becomes_required_even_if_feature_flags_are_off(tmp_path):
    settings = _settings(tmp_path)
    _sqlite(settings.effective_fleet_control_path())

    members = {
        item["member_id"]: item for item in authoritative_backup_manifest(settings)["members"]
    }
    assert members["fleet_node_meta"]["required"] is True
    assert members["fleet_control"]["required"] is True


def test_validation_accepts_complete_standalone_state_and_optional_runtime_config(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)

    result = validate_authoritative_backup_source(settings)
    rows = {item["member_id"]: item for item in result["validated"]}

    assert rows["runtime_database"]["user_version"] == 19
    assert rows["auth_database"]["user_version"] == 3
    assert rows["access_verifier_key"]["mode"] == "0600"
    assert "runtime_config" not in rows


def test_validation_requires_fleet_pair_when_fleet_state_exists(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    _sqlite(settings.effective_fleet_control_path(), 3)

    with pytest.raises(BackupValidationError, match="fleet_node_meta"):
        validate_authoritative_backup_source(settings)


def test_validation_fails_when_access_verifier_key_is_missing(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    access_verifier_key_path(settings.auth_database_path).unlink()

    with pytest.raises(BackupValidationError, match="access_verifier_key"):
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


def test_validation_rejects_path_alias_between_authoritative_members(tmp_path):
    settings = _settings(tmp_path)
    _complete_source(settings)
    aliased = settings.model_copy(update={"auth_database_path": settings.database_path})

    with pytest.raises(BackupValidationError, match="resolve to the same path"):
        validate_authoritative_backup_source(aliased)

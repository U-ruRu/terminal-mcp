"""Independent bootstrap identity; neighboring topology storage is never created."""

import sqlite3

import pytest

from terminal_mcp.config import Settings
from terminal_mcp.node_identity import ensure_node_identity, local_namespace


def settings(path):
    return Settings(_env_file=None, database_path=path / "runtime.sqlite3")


def test_bootstrap_credentials_survive_a_new_settings_instance(tmp_path):
    first = settings(tmp_path)
    ensure_node_identity(first)
    restarted = settings(tmp_path)
    ensure_node_identity(restarted)

    assert (
        restarted.fleet_instance_id,
        restarted.fleet_signing_private_key,
        restarted.access_mesh_proof_key,
    ) == (first.fleet_instance_id, first.fleet_signing_private_key, first.access_mesh_proof_key)


def test_bootstrap_creates_local_namespace_without_mesh_membership(tmp_path):
    standalone = settings(tmp_path)
    ensure_node_identity(standalone)

    assert standalone.fleet_id == local_namespace(standalone.fleet_node_id)
    assert standalone.fleet_peers_json == "[]"
    assert not standalone.effective_fleet_control_path().exists()


def test_separate_installations_have_isolated_registration_credentials(tmp_path):
    first, second = settings(tmp_path / "a"), settings(tmp_path / "b")
    ensure_node_identity(first)
    ensure_node_identity(second)

    assert first.fleet_id != second.fleet_id
    assert first.access_mesh_proof_key != second.access_mesh_proof_key


def test_missing_established_identity_cannot_generate_new_mesh_authority(tmp_path):
    configured = settings(tmp_path)
    with sqlite3.connect(configured.database_path) as db:
        db.execute("CREATE TABLE unified_runtime_migration(singleton INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="durable node identity is missing"):
        ensure_node_identity(configured)
    assert not configured.database_path.with_name("node-identity.json").exists()

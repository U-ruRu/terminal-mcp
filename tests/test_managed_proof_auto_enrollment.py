"""Native Access proof enrollment from authenticated managed authority material."""

import pytest

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.storage.access_mesh import AccessMeshStore

FIRST = b"original-node-bootstrap-secret-firstbyte"
MAIN = b"original-node-bootstrap-secret-main-000"
AUTHORITY_TOKEN = "authority-ingress-token-" + "t" * 48
WRONG_TOKEN = "wrong-authority-ingress-token-" + "v" * 48


def store(path, local, bootstrap):
    return AccessMeshStore(
        path,
        local_node_id=local,
        trusted_issuers=frozenset(("bacloud", "firstbyte", "main")),
        proof_key=bootstrap,
    )


def test_managed_enrollment_derives_shared_authority_proof(tmp_path):
    first = store(tmp_path / "first.sqlite3", "firstbyte", FIRST)
    second = store(tmp_path / "second.sqlite3", "main", MAIN)
    first.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    second.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    assert second.code_tag("bacloud", "0123") == first.code_tag("bacloud", "0123")


def test_authority_rotation_preserves_existing_local_proof(tmp_path):
    mesh = store(tmp_path / "mesh.sqlite3", "main", MAIN)
    mesh.install_managed_proof(
        mesh_id="mesh-a", authority_id="bacloud", ingress_token=AUTHORITY_TOKEN
    )
    original = mesh.code_tag("main", "7654")
    mesh.install_managed_proof(
        mesh_id="mesh-b", authority_id="firstbyte", ingress_token=WRONG_TOKEN
    )
    reopened = store(tmp_path / "mesh.sqlite3", "main", MAIN)
    assert original in reopened.code_tags("main", "7654")


def test_pending_membership_rejects_remote_bootstrap_claim(tmp_path):
    mesh = store(tmp_path / "pending.sqlite3", "main", MAIN)
    mesh.managed_trust_required = True
    with pytest.raises(AccessMeshError, match="access_mesh_proof_enrollment_pending"):
        mesh.attach(issuer_id="bacloud", code="5678", connection_key="fixture-pending")

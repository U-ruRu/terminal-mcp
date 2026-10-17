"""Isolated settings and PKCE values for the current OAuth router tests."""

import base64
import hashlib

from terminal_mcp.config import Settings


def pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def settings(tmp_path, **overrides):
    data = dict(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        access_mesh_enabled=True,
        fleet_instance_id="test-node",
        fleet_signing_private_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        fleet_peers_json="[]",
        access_mesh_proof_key="mesh-runtime-fixture-key-never-production",
        access_mesh_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
        persistent_agents_enabled=True,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        oauth_issuer="https://terminal.example",
        oauth_audience="https://terminal.example/mcp",
        oauth_signing_secret="test-secret-that-is-at-least-32-bytes",
        oauth_admin_username="admin",
        oauth_admin_password="secret",
        oauth_users_json="[]",
        bearer_credentials_json="[]",
        mcp_auth_mode="",
        actions_auth_mode="",
    )
    data.update(overrides)
    return Settings(**data)

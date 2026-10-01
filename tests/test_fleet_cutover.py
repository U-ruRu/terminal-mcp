import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return (
        _encoded(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ),
        _encoded(
            private.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ),
    )


def test_legacy_mesh_can_retire_while_v1_and_persistent_keep_peer_auth(tmp_path):
    private, _ = _keypair()
    _, peer_public = _keypair()
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "runtime.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://node-a.example",
        fleet_instance_id="node-a",
        fleet_signing_private_key=private,
        fleet_peers_json=json.dumps(
            [
                {
                    "instance_id": "node-b",
                    "origin": "https://node-b.example",
                    "public_key": peer_public,
                    "auth_token": "peer-token",
                }
            ]
        ),
        fleet_legacy_replication_enabled=False,
        fleet_v1_source_enabled=True,
        fleet_id="fleet-a",
        fleet_node_id="node-a",
        persistent_agents_enabled=True,
        legacy_agent_admission_enabled=False,
    )

    app = create_app(settings)

    assert app.state.fleet_replication is not None
    assert app.state.legacy_fleet_replication is None
    assert app.state.service.fleet_replication is None
    assert app.state.fleet_replication.authenticate("node-b", "Bearer peer-token") is not None

    with TestClient(app, base_url="https://node-a.example") as client:
        assert client.get("/internal/fleet/identities").status_code == 404
        assert client.post("/internal/fleet/session-update", json={}).status_code == 404
        assert client.post("/internal/fleet/session-finish", json={}).status_code == 404
        assert client.get("/internal/fleet/v1/source/manifest").status_code == 401
        assert client.post("/internal/fleet/persistent/permit", json={}).status_code == 401

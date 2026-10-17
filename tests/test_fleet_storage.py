import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import build_fleet_config


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return encoded(private_raw), encoded(public_raw)


def settings(**changes):
    values = {
        "fleet_instance_id": "",
        "fleet_signing_private_key": "",
        "fleet_peers_json": "[]",
        "fleet_replication_interval_sec": 5.0,
        "fleet_request_timeout_sec": 3.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_managed_fleet_configuration_loads_the_configured_peer():
    private, _ = keypair()
    _, peer_public = keypair()
    config = build_fleet_config(
        settings(
            fleet_instance_id="server-a",
            fleet_signing_private_key=private,
            fleet_peers_json=(
                '[{"instance_id":"server-b","origin":"https://server-b.example.invalid",'
                f'"public_key":"{peer_public}","auth_token":"placeholder-peer-token"}}]'
            ),
        )
    )
    assert config is not None
    assert config.instance_id == "server-a"
    assert config.peers[0].origin == "https://server-b.example.invalid"


def test_fleet_configuration_requires_tls_for_remote_peers():
    private, _ = keypair()
    _, peer_public = keypair()
    peer = (
        '[{"instance_id":"server-b","origin":"http://server-b.example.invalid",'
        f'"public_key":"{peer_public}","auth_token":"placeholder-peer-token"}}]'
    )
    with pytest.raises(ValueError, match="HTTPS"):
        build_fleet_config(
            settings(
                fleet_instance_id="server-a",
                fleet_signing_private_key=private,
                fleet_peers_json=peer,
            )
        )

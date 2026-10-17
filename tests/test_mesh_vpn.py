"""Opt-in WireGuard bootstrap: signed enrollment, isolated routing and rollback."""

import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.mesh_vpn import (
    VPNError,
    _backend,
    _endpoint,
    _load,
    create_offer,
    fingerprint,
    switch,
    up,
    verify_offer,
)

PRIVATE = base64.b64encode(bytes(range(32))).decode()
PUBLIC = base64.b64encode(bytes(range(32, 64))).decode()


def signer():
    private = Ed25519PrivateKey.generate()
    key = base64.urlsafe_b64encode(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public = base64.urlsafe_b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    return key, public


def local(node, overlay, endpoint):
    return {
        "version": 1,
        "instance_id": node,
        "backend": "auto",
        "interface": "tmcpwg",
        "overlay_ip": overlay,
        "endpoint": endpoint,
        "listen_port": 53148,
        "proxy_port": 18080,
        "wireguard_public_key": PUBLIC,
    }


def test_signed_enrollment_rejects_tampered_payload(tmp_path):
    key, public = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), key, now=1000)
    offer["payload"]["overlay_ip"] = "10.244.12.90"
    with pytest.raises(VPNError, match="signed enrollment"):
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=public, now=1100)


def test_enrollment_requires_current_pinned_fleet_identity():
    key, _ = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), key, now=1000)
    with pytest.raises(VPNError):
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=signer()[1], now=1100)


def test_expired_enrollment_cannot_join_fleet():
    key, public = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), key, now=1000)
    with pytest.raises(VPNError, match="expired"):
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=public, now=1700)


@pytest.mark.parametrize("kernel_available,expected", [(True, "kernel"), (False, "userspace")])
def test_automatic_vpn_backend_uses_available_driver(monkeypatch, kernel_available, expected):
    monkeypatch.setattr("terminal_mcp.mesh_vpn._run", lambda *cmd: b"")
    monkeypatch.setattr("terminal_mcp.mesh_vpn.shutil.which", lambda cmd: "/usr/bin/wireguard-go")
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0 if kernel_available else 1),
    )
    assert _backend("tmcpwg", "auto") == expected


def test_up_creates_only_narrow_routes_and_never_default(monkeypatch, tmp_path):
    root = tmp_path / "identity"
    (root / "peers").mkdir(parents=True)
    (root / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    (root / "private.key").write_text(PRIVATE)
    (root / "peers" / "firstbyte.json").write_text(
        json.dumps(
            create_offer(
                local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), signer()[0], now=1_000
            )["payload"]
        )
    )
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr("terminal_mcp.mesh_vpn._backend", lambda *args: "kernel")
    seen = []
    monkeypatch.setattr("terminal_mcp.mesh_vpn._run", lambda *args: seen.append(args) or b"")
    assert up(root) == "kernel"
    assert ("ip", "route", "add", "10.244.12.1/32", "dev", "tmcpwg") in seen
    assert not any("default" in args or "0.0.0.0/0" in args for args in seen)
    assert any(args[:3] == ("wg", "set", "tmcpwg") for args in seen)


def test_detached_peer_removes_routes_keys_and_transport(tmp_path, monkeypatch):
    from terminal_mcp.mesh_vpn import prune_detached_peers

    directory = tmp_path / "vpn"
    (directory / "peers").mkdir(parents=True)
    (directory / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    (directory / "peers" / "firstbyte.json").write_text(
        json.dumps(
            create_offer(
                local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), signer()[0], now=1_000
            )["payload"]
        )
    )
    overrides = tmp_path / "transports.json"
    overrides.write_text(
        json.dumps(
            {
                "firstbyte": {
                    "mode": "wireguard",
                    "origin": "http://10.244.12.1:18080",
                    "interface": "tmcpwg",
                }
            }
        )
    )
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert prune_detached_peers(directory, overrides, set()) == ["firstbyte"]
    assert not (directory / "peers" / "firstbyte.json").exists()
    assert _load(overrides) == {}


def test_healthy_wireguard_tunnel_becomes_selected_transport(tmp_path, monkeypatch):
    """A sporadically passing tunnel cannot be promoted into the live Mesh route."""
    root = tmp_path / "mesh"
    (root / "peers").mkdir(parents=True)
    (root / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    (root / "peers" / "firstbyte.json").write_text(
        json.dumps(
            create_offer(
                local("firstbyte", "10.244.12.1", "185.244.172.75:42063"),
                signer()[0],
                now=1000,
            )["payload"]
        )
    )
    marker = fingerprint(PUBLIC)[:12]
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.status", lambda _: {"running": True, "handshakes": {marker: 100}}
    )
    attempted = []

    def success(*args, **kwargs):
        attempted.append((args, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("terminal_mcp.mesh_vpn.httpx.get", success)
    selected = tmp_path / "routes.json"
    assert (
        switch(root, selected, peer_id="firstbyte", mode="wireguard", now=101)["transport"]
        == "wireguard"
    )
    assert len(attempted) == 8
    assert all(item[1]["trust_env"] is False for item in attempted)


def test_managed_transport_refresh_preserves_pinned_access_identity():
    from terminal_mcp.fleet.access_mesh import AccessMeshReplication
    from terminal_mcp.fleet.config import FleetConfig, FleetPeer
    from terminal_mcp.fleet.control_plane import ManagedFleetControl

    pinned = FleetPeer("bacloud", "https://public-bac.example", "pinned-signing", "pinned-token")
    initial = FleetConfig("firstbyte", "signing-key", (pinned,), 5.0, 3.0)
    store = SimpleNamespace(trusted_issuers=frozenset({"bacloud"}))
    transport = AccessMeshReplication(SimpleNamespace(store=store), initial)
    controller = ManagedFleetControl(
        SimpleNamespace(),
        initial,
        None,
        public_base_url="https://firstbyte.example",
        runtime_targets=(transport,),
    )
    active = FleetConfig(
        "firstbyte",
        "new-managed-signing-key",
        (
            FleetPeer(
                "bacloud",
                "http://10.253.242.2:18084",
                "untrusted-managed-key",
                "untrusted-managed-token",
                "wireguard",
            ),
            FleetPeer("newly-added", "http://10.253.242.3:18084", "k", "t", "wireguard"),
        ),
        5.0,
        3.0,
    )
    controller._replace_runtime_config(active)
    assert len(transport.peers) == 1
    peer = transport.peers[0]
    assert peer.instance_id == "bacloud"
    assert peer.origin == "http://10.253.242.2:18084"
    assert peer.auth_token == pinned.auth_token
    assert peer.public_key == pinned.public_key
    assert transport.config.signing_private_key == initial.signing_private_key


@pytest.mark.parametrize(
    ("provided", "canonical"),
    [
        ("185.244.172.75:61063", "185.244.172.75:61063"),
        ("[2A04:2181:C011:1::BAFB:F78E]:42065", "[2a04:2181:c011:1::bafb:f78e]:42065"),
    ],
)
def test_enrollment_accepts_numeric_ipv4_and_global_ipv6_underlay(provided, canonical):
    private, public = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", provided), private, now=1000)
    assert (
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=public, now=1020)[
            "endpoint"
        ]
        == provided
    )


@pytest.mark.parametrize(
    "value",
    [
        "2a04:5200:fff5::344a:61063",
        "[fe80::1]:61063",
        "[::ffff:185.244.172.75]:61063",
        "[2a04:5200:fff5::344a%eth0]:61063",
        "[2a04:5200:fff5::344a]:0",
        "bad.example:61063",
    ],
)
def test_ipv6_endpoint_rejects_non_global_ambiguous_or_dns_address(value):
    with pytest.raises(VPNError):
        _endpoint(value)

"""Opt-in WireGuard bootstrap: signed enrollment, isolated routing and rollback."""

import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.fleet.config import build_fleet_config, load_peer_transports
from terminal_mcp.mesh_vpn import (
    VPNError,
    _backend,
    _load,
    create_offer,
    fingerprint,
    init,
    install_units,
    join,
    set_backend,
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


def test_signed_bootstrap_requires_pinned_fleet_identity_and_expiry(tmp_path):
    key, public = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), key, now=1_000)
    payload = verify_offer(offer, expected_instance_id="firstbyte", pinned_key=public, now=1_100)
    assert payload["instance_id"] == "firstbyte"
    with pytest.raises(VPNError, match="identity mismatch"):
        verify_offer(offer, expected_instance_id="bacloud", pinned_key=public, now=1_100)
    with pytest.raises(VPNError, match="expired"):
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=public, now=1_700)
    with pytest.raises(VPNError, match="fingerprint"):
        verify_offer(offer, expected_instance_id="firstbyte", now=1_100)
    assert (
        verify_offer(
            offer,
            expected_instance_id="firstbyte",
            approved_fingerprint=fingerprint(payload["fleet_signing_public_key"]),
            now=1_100,
        )
        == payload
    )
    forged = json.loads(json.dumps(offer))
    forged["payload"]["overlay_ip"] = "10.244.12.90"
    with pytest.raises(VPNError, match="signed enrollment"):
        verify_offer(forged, expected_instance_id="firstbyte", pinned_key=public, now=1_100)
    with pytest.raises(VPNError):
        verify_offer(offer, expected_instance_id="firstbyte", pinned_key=signer()[1], now=1_100)


def test_join_durable_bootstrap_and_rejects_key_rotation(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    key, public = signer()
    offer = create_offer(local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), key, now=1_000)
    joined = join(
        root, offer, peer_id="firstbyte", pinned_key=public, approved_fingerprint=None, now=1_100
    )
    assert _load(root / "peers" / "firstbyte.json") == joined
    assert not (root / "private.key").exists()
    assert (
        join(
            root,
            offer,
            peer_id="firstbyte",
            pinned_key=public,
            approved_fingerprint=None,
            now=1_100,
        )
        == joined
    )
    changed = local("firstbyte", "10.244.12.3", "185.244.172.75:53148")
    other = create_offer(changed, key, now=1_020)
    with pytest.raises(VPNError, match="changed"):
        join(
            root,
            other,
            peer_id="firstbyte",
            pinned_key=public,
            approved_fingerprint=None,
            now=1_100,
        )


def test_backend_selection_kernel_userspace_and_fail_closed(monkeypatch):
    calls = []
    monkeypatch.setattr("terminal_mcp.mesh_vpn._run", lambda *cmd: calls.append(cmd))
    monkeypatch.setattr("terminal_mcp.mesh_vpn.shutil.which", lambda cmd: "/usr/bin/wireguard-go")
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    assert _backend("tmcpwg", "auto") == "kernel"
    assert calls == []
    assert _backend("tmcpwg", "userspace") == "userspace"
    assert calls == [("wireguard-go", "tmcpwg")]
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert _backend("tmcpwg", "auto") == "userspace"
    with pytest.raises(VPNError, match="kernel"):
        _backend("tmcpwg", "kernel")


def test_init_never_outputs_private_key_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn._run",
        lambda *args, **kwargs: (PRIVATE if args == ("wg", "genkey") else PUBLIC).encode(),
    )
    state = tmp_path / "identity"
    created = init(
        state,
        instance_id="firstbyte",
        backend="auto",
        interface="tmcpwg",
        overlay_ip="10.244.12.1",
        endpoint="185.244.172.75:53148",
        listen_port=53148,
        proxy_port=18080,
    )
    assert created["wireguard_public_key"] == PUBLIC
    assert "private" not in json.dumps(created).lower()
    assert (state / "private.key").read_text() == PRIVATE + "\n"
    assert (state / "private.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(VPNError, match="initialized"):
        init(
            state,
            instance_id="firstbyte",
            backend="auto",
            interface="tmcpwg",
            overlay_ip="10.244.12.1",
            endpoint="185.244.172.75:53148",
            listen_port=53148,
            proxy_port=18080,
        )
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert set_backend(state, "userspace")["backend"] == "userspace"
    assert _load(state / "local.json")["backend"] == "userspace"


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


def test_peer_transport_switch_validation_and_https_rollback(tmp_path):
    root = tmp_path / "mesh"
    (root / "peers").mkdir(parents=True)
    (root / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    (root / "peers" / "firstbyte.json").write_text(
        json.dumps(
            create_offer(
                local("firstbyte", "10.244.12.1", "185.244.172.75:53148"), signer()[0], now=1_000
            )["payload"]
        )
    )
    path = tmp_path / "transports.json"
    assert switch(root, path, peer_id="firstbyte", mode="wireguard", probe=False) == {
        "peer": "firstbyte",
        "transport": "wireguard",
        "restart_required": True,
    }
    config = load_peer_transports(str(path))
    assert config["firstbyte"]["origin"] == "http://10.244.12.1:18080"
    assert switch(root, path, peer_id="firstbyte", mode="https")["transport"] == "https"
    assert _load(path) == {}
    path.write_text(
        '{"firstbyte":{"mode":"wireguard","origin":"http://8.8.8.8:18080","interface":"tmcpwg"}}'
    )
    with pytest.raises(ValueError, match="private"):
        load_peer_transports(str(path))


def test_fleet_uses_vpn_only_for_selected_peer(tmp_path):
    key, public = signer()
    path = tmp_path / "peer-transports.json"
    path.write_text(
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
    settings = SimpleNamespace(
        fleet_instance_id="bacloud",
        fleet_signing_private_key=key,
        fleet_peers_json=json.dumps(
            [
                {
                    "instance_id": "firstbyte",
                    "origin": "https://terminal-fb.elenis.org",
                    "public_key": public,
                    "auth_token": "token-fb",
                },
                {
                    "instance_id": "secondary",
                    "origin": "https://secondary.elenis.org",
                    "public_key": public,
                    "auth_token": "token-secondary",
                },
            ]
        ),
        fleet_peer_transports_path=str(path),
        fleet_replication_interval_sec=5,
        fleet_request_timeout_sec=3,
    )
    conf = build_fleet_config(settings)
    assert conf.peers_by_id["firstbyte"].origin == "http://10.244.12.1:18080"
    assert conf.peers_by_id["firstbyte"].bootstrap_origin == "https://terminal-fb.elenis.org"
    assert conf.peers_by_id["firstbyte"].transport == "wireguard"
    assert conf.peers_by_id["secondary"].origin == "https://secondary.elenis.org"
    assert conf.peers_by_id["secondary"].transport == "https"


def test_service_install_generates_opt_in_units_without_activation(tmp_path):
    root = tmp_path / "identity"
    root.mkdir()
    (root / "local.json").write_text(
        json.dumps(local("bacloud", "10.244.12.2", "88.119.171.230:53148"))
    )
    units = tmp_path / "systemd"
    names = install_units(root, units, "/usr/local/bin/terminal-mcp")
    assert len(names) == 2
    proxy = (units / "terminal-mcp-mesh-vpn-proxy.service").read_text()
    assert "NoNewPrivileges=yes" in proxy
    assert "terminal-mcp-mesh-vpn.service" in proxy
    assert install_units(root, units, "/usr/local/bin/terminal-mcp") == names
    with pytest.raises(VPNError, match="different content"):
        install_units(root, units, "/another/path")

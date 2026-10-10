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
    assert len(names) == 3
    proxy = (units / "terminal-mcp-mesh-vpn-proxy.service").read_text()
    assert "NoNewPrivileges=yes" in proxy
    assert "terminal-mcp-mesh-vpn.service" in proxy
    route_recovery = (units / "terminal-mcp-mesh-vpn-routes.service").read_text()
    assert "mesh-vpn watch" in route_recovery
    assert "CAP_NET_ADMIN" in route_recovery
    assert install_units(root, units, "/usr/local/bin/terminal-mcp") == names
    with pytest.raises(VPNError, match="different content"):
        install_units(root, units, "/another/path")


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
    assert prune_detached_peers(directory, overrides, set()) == []


def test_dynamic_managed_peer_uses_transport_override(tmp_path):
    from terminal_mcp.fleet.config import FleetPeer, select_peer_transport

    public_origin = "https://external.example"
    original = FleetPeer("remote", public_origin, "trusted-signing-key", "trusted-bearer")
    overrides = tmp_path / "mesh-transports.json"
    overrides.write_text(
        json.dumps(
            {
                "remote": {
                    "mode": "wireguard",
                    "origin": "http://10.244.12.1:18080",
                    "interface": "tmcpwg",
                }
            }
        )
    )
    peer = select_peer_transport(original, str(overrides))
    assert peer.origin == "http://10.244.12.1:18080"
    assert peer.bootstrap_origin == public_origin
    assert peer.public_key == original.public_key
    assert peer.auth_token == original.auth_token
    assert select_peer_transport(original, "") == original
    assert select_peer_transport(original, str(tmp_path / "missing.json")) == original


def test_mobile_operator_api_enforces_managed_peer_and_pinned_key(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from terminal_mcp.http.mesh_vpn_control import build_mesh_vpn_control_router

    private, public = signer()
    root = tmp_path / "vpn"
    transports = tmp_path / "routes.json"

    class Controller:
        config = SimpleNamespace(instance_id="bacloud", signing_private_key=private)

        def __init__(self):
            self.reconciles = 0

        async def reconcile_local(self):
            self.reconciles += 1

    controller = Controller()

    class Application:
        async def state(self, actor):
            assert actor.endpoint_role == "operator"
            return {
                "ok": True,
                "control": {
                    "managed": True,
                    "nodes": [
                        {"node_id": "bacloud", "mesh_id": "prod", "state": "active"},
                        {
                            "node_id": "firstbyte",
                            "mesh_id": "prod",
                            "state": "active",
                            "public_key": public,
                        },
                        {
                            "node_id": "other",
                            "mesh_id": "unrelated",
                            "state": "active",
                            "public_key": public,
                        },
                    ],
                    "revisions": {"topology": 3},
                },
            }

    settings = SimpleNamespace(
        mesh_vpn_state_dir=str(root), fleet_peer_transports_path=str(transports)
    )
    monkeypatch.setattr(
        "terminal_mcp.http.mesh_vpn_control.ActorContext.from_admission",
        lambda *args, **kwargs: SimpleNamespace(endpoint_role="operator"),
    )
    monkeypatch.setattr(
        "terminal_mcp.http.mesh_vpn_control.current_admission_context", lambda: None
    )
    monkeypatch.setattr(
        "terminal_mcp.mesh_vpn._run",
        lambda *args, **kwargs: (PRIVATE if args == ("wg", "genkey") else PUBLIC).encode(),
    )
    app = FastAPI()
    app.include_router(build_mesh_vpn_control_router(controller, Application(), settings))
    with TestClient(app) as browser:
        base = "/actions/fleet/control/transport"
        assert browser.get(base).json()["prepared"] is False
        prepared = browser.post(
            base + "/prepare",
            json={
                "backend": "kernel",
                "overlay_ip": "10.244.12.2",
                "endpoint": "88.119.171.230:53148",
            },
        ).json()
        assert prepared["ok"] is True
        assert browser.get(base).json()["offer"]["payload"]["instance_id"] == "bacloud"
        remote = create_offer(
            local("firstbyte", "10.244.12.1", "185.244.172.75:53148"),
            private,
            now=int(__import__("time").time()),
        )
        assert (
            browser.post(base + "/enroll", json={"peer_id": "other", "offer": remote}).json()["ok"]
            is False
        )
        assert (
            browser.post(base + "/enroll", json={"peer_id": "firstbyte", "offer": remote}).json()[
                "ok"
            ]
            is True
        )
        response = browser.post(
            base + "/switch",
            json={"peer_id": "firstbyte", "mode": "wireguard", "expected_topology_revision": 2},
        ).json()
        assert response["ok"] is False
        assert controller.reconciles == 0
        assert (
            browser.post(
                base + "/switch",
                json={"peer_id": "firstbyte", "mode": "https", "expected_topology_revision": 3},
            ).json()["ok"]
            is True
        )
        assert controller.reconciles == 1
        assert browser.post(base + "/revoke", json={"peer_id": "firstbyte"}).json()["ok"]
        assert controller.reconciles == 2


def test_wireguard_cutover_requires_eight_consecutive_private_health_passes(tmp_path, monkeypatch):
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

    selected.unlink()
    attempted.clear()

    def sporadic(*args, **kwargs):
        attempted.append((args, kwargs))
        return SimpleNamespace(status_code=503 if len(attempted) == 4 else 200)

    monkeypatch.setattr("terminal_mcp.mesh_vpn.httpx.get", sporadic)
    with pytest.raises(VPNError, match="health probe failed"):
        switch(root, selected, peer_id="firstbyte", mode="wireguard", now=101)
    assert len(attempted) == 4
    assert not selected.exists()

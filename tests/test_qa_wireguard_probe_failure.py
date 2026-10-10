"""A failed live WireGuard probe must return a structured operator error."""

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.http.mesh_vpn_control import build_mesh_vpn_control_router
from terminal_mcp.mesh_vpn import fingerprint


def test_operator_switch_reports_unreachable_wireguard_proxy(tmp_path: Path):
    root = tmp_path / "wireguard"
    peers = root / "peers"
    peers.mkdir(parents=True)
    public = base64.b64encode(bytes(range(32, 64))).decode()
    local = {
        "instance_id": "bacloud",
        "interface": "tmcpwg",
        "overlay_ip": "10.244.12.2",
        "wireguard_public_key": public,
    }
    peer = {
        "instance_id": "firstbyte",
        "wireguard_public_key": public,
        "overlay_ip": "10.244.12.1",
        "proxy_port": 18080,
    }
    (root / "local.json").write_text(json.dumps(local))
    (peers / "firstbyte.json").write_text(json.dumps(peer))
    target = tmp_path / "overrides.json"

    class Controller:
        config = SimpleNamespace(instance_id="bacloud")

        async def reconcile_local(self):
            raise AssertionError("Failed probe must not trigger runtime reconciliation")

    class Application:
        async def state(self, _actor):
            return {
                "ok": True,
                "control": {
                    "managed": True,
                    "nodes": [
                        {"node_id": "bacloud", "mesh_id": "mesh", "state": "active"},
                        {
                            "node_id": "firstbyte",
                            "mesh_id": "mesh",
                            "state": "active",
                            "public_key": public,
                        },
                    ],
                    "revisions": {"topology": 1},
                },
            }

    settings = SimpleNamespace(mesh_vpn_state_dir=str(root), fleet_peer_transports_path=str(target))
    app = FastAPI()
    app.include_router(build_mesh_vpn_control_router(Controller(), Application(), settings))
    handshake = {"running": True, "handshakes": {fingerprint(public)[:12]: int(time.time())}}
    with (
        patch(
            "terminal_mcp.http.mesh_vpn_control.ActorContext.from_admission",
            return_value=SimpleNamespace(endpoint_role="operator"),
        ),
        patch("terminal_mcp.http.mesh_vpn_control.current_admission_context", return_value=None),
        patch("terminal_mcp.mesh_vpn.status", return_value=handshake),
        patch("terminal_mcp.mesh_vpn.httpx.get", side_effect=httpx.ConnectError("proxy refused")),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.post(
            "/actions/fleet/control/transport/switch",
            json={"peer_id": "firstbyte", "mode": "wireguard"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["code"] == "vpn_unavailable"
    assert not target.exists()

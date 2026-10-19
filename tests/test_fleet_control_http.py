from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.core.persistent_policy import PersistentPolicyError
from terminal_mcp.http.fleet_control import build_fleet_control_router
from terminal_mcp.http.mesh_vpn_control import build_mesh_vpn_control_router


class PolicyBlockedController:
    async def update_policy(self, **kwargs):
        raise PersistentPolicyError(
            "policy_in_use",
            blockers=[
                {
                    "logical_agent_id": "la_busy",
                    "display_name": "Busy slot",
                    "state": "armed",
                }
            ],
        )


@pytest.mark.parametrize("action", ["prepare", "enroll", "activate", "backend", "switch", "revoke"])
def test_windows_transport_rejects_linux_provisioning_before_os_calls(
    monkeypatch, tmp_path, action
):
    import terminal_mcp.http.mesh_vpn_control as module

    monkeypatch.setattr(module, "IS_WINDOWS", True)
    app = FastAPI()
    settings = SimpleNamespace(
        mesh_vpn_state_dir=str(tmp_path), fleet_peer_transports_path=tmp_path / "routes.json"
    )
    app.include_router(build_mesh_vpn_control_router(None, None, settings))
    with TestClient(app) as client:
        response = client.post("/actions/fleet/control/transport/" + action, json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "vpn_platform_unsupported"


def test_fleet_policy_in_use_is_returned_as_structured_control_error():
    app = FastAPI()
    app.include_router(build_fleet_control_router(PolicyBlockedController(), None))

    with TestClient(app) as client:
        response = client.post(
            "/actions/fleet/control/policy",
            json={
                "duration_seconds": 180,
                "warning_after_seconds": 60,
                "alert_after_seconds": 120,
                "rearm_after_seconds": 15,
                "expected_revision": 1,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": False,
        "code": "policy_in_use",
        "error": "policy_in_use",
        "blockers": [
            {
                "logical_agent_id": "la_busy",
                "display_name": "Busy slot",
                "state": "armed",
            }
        ],
    }

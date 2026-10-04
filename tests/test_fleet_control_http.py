from fastapi import FastAPI
from fastapi.testclient import TestClient

from terminal_mcp.core.persistent_policy import PersistentPolicyError
from terminal_mcp.http.fleet_control import build_fleet_control_router


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
                "legacy_admission_enabled": False,
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

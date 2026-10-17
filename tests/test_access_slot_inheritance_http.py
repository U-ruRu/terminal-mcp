"""Real operator HTTP -> policy storage -> current-window/replica inheritance.

All applications, identities and codes are synthetic and held in temporary files.
Mutation receipts containing codes are deliberately omitted from assertion messages.
"""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS
from test_android_slot_http_window_qa import read_slot

from terminal_mcp.app import create_app


def mutate(client, action, key, **values):
    if (
        action == "policy"
        and values.get("inherit_policy") is True
        and "expected_parent_revision" not in values
    ):
        values["expected_parent_revision"] = defaults(client)["revision"]
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={"action": action, "idempotency_key": key, **values},
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("ok") is True
    return body


def defaults(client):
    response = client.get("/actions/access/defaults", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body.get("ok") is True
    return body


def create(client, key, **kwargs):
    return mutate(client, "create", key, mode="persistent", **kwargs)


def change_defaults(client, key, **kwargs):
    return mutate(client, "defaults", key, expected_revision=defaults(client)["revision"], **kwargs)


def mesh_config(tmp_path, control="firstbyte"):
    config = settings(tmp_path)
    return type(config).model_validate(
        {
            **config.model_dump(),
            "fleet_id": "qa-inheritance",
            "fleet_v1_authority_enabled": True,
            "fleet_v1_source_enabled": True,
            "fleet_node_id": "firstbyte",
            "fleet_peer_transports_path": str(tmp_path / "peer-transports.json"),
            "mesh_vpn_state_dir": str(tmp_path / "mesh-vpn"),
            "fleet_control_node_id": control,
            "fleet_control_path": tmp_path / "fleet.sqlite3",
        }
    )


def fleet_policy(client, duration, *, expected=None):
    state = client.get("/actions/fleet/control", headers=HEADERS).json()["control"]
    response = client.post(
        "/actions/fleet/control/policy",
        headers=HEADERS,
        json={
            "duration_seconds": duration,
            "warning_after_seconds": duration - 180,
            "alert_after_seconds": duration - 60,
            "rearm_after_seconds": 180,
            "legacy_admission_enabled": True,
            "expected_revision": state["policy"]["revision"] if expected is None else expected,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_reset_request_requires_explicit_parent_revision(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "parent-required")
        response = client.post(
            "/actions/access/mutate",
            headers=HEADERS,
            json={
                "action": "policy",
                "slot_id": issued["slot_id"],
                "expected_revision": 1,
                "inherit_policy": True,
                "idempotency_key": "missing-parent-proof",
            },
        )
        assert response.status_code == 200
        assert response.json().get("ok") is False
        assert response.json().get("code") == "validation_error"
        assert read_slot(client, issued["slot_id"])["revision"] == 1

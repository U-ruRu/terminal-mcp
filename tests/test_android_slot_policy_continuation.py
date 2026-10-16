"""HTTP regressions for future defaults, current windows and durable retry.

Only temporary applications, test credentials and synthetic slots are used.
Code-bearing creation receipts are never printed in assertion diagnostics.
"""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS, internal_persistent_start
from test_android_slot_http_window_qa import T0, provision, read_slot

from terminal_mcp.app import create_app


def set_clock(app, stamp):
    app.state.access_mesh.clock = lambda: stamp
    app.state.access_mesh.store.clock = lambda: stamp


def post(client, body):
    response = client.post("/actions/access/mutate", headers=HEADERS, json=body)
    assert response.status_code == 200
    result = response.json()
    assert result.get("ok") is True
    return result


def next_policy(slot, key):
    return {
        "action": "policy",
        "idempotency_key": key,
        "slot_id": slot["slot_id"],
        "expected_revision": slot["revision"],
        "policy": {
            "duration_seconds": 10800,
            "warning_seconds": 600,
            "draining_seconds": 120,
            "cooldown_seconds": 5,
        },
    }


def test_stale_policy_revision_cannot_overwrite_confirmed_policy_or_window(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = provision(client, "uniform-policy-conflict")
        assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
        before = read_slot(client, issued["slot_id"])
        post(client, next_policy(before, "uniform-policy-first"))
        saved = read_slot(client, issued["slot_id"])
        stale = next_policy(before, "uniform-policy-stale-second")
        stale["policy"]["duration_seconds"] = 18000
        rejected = client.post("/actions/access/mutate", headers=HEADERS, json=stale)
        assert rejected.status_code == 200
        assert rejected.json().get("ok") is False
        assert rejected.json().get("code") == "revision_conflict"
        assert read_slot(client, issued["slot_id"]) == saved

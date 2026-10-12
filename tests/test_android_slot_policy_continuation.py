"""HTTP regressions for future defaults, current windows and durable retry.

Only temporary applications, test credentials and synthetic slots are used.
Code-bearing creation receipts are never printed in assertion diagnostics.
"""

from datetime import timedelta

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


def test_http_future_policy_preserves_active_thresholds_and_survives_restart(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = provision(client, "uniform-policy-restart")
        assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
        before = read_slot(client, issued["slot_id"])
        set_clock(app, T0 + timedelta(seconds=60))
        body = next_policy(before, "uniform-policy-stable-retry")
        result = post(client, body)
        assert post(client, body) == result
        updated = read_slot(client, issued["slot_id"])
        assert updated["revision"] == before["revision"] + 1
        assert updated["policy"]["duration_seconds"] == 10800
        assert (
            updated["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )
        # Current thresholds must remain at the original 180/90 seconds.
        set_clock(app, T0 + timedelta(seconds=1201))
        assert read_slot(client, issued["slot_id"])["session_lifecycle"]["state"] == "warning"
    restarted = create_app(config)
    with TestClient(restarted, base_url="https://terminal.example") as client:
        set_clock(restarted, T0 + timedelta(seconds=1201))
        persisted = read_slot(client, issued["slot_id"])
        assert persisted["revision"] == updated["revision"]
        assert (
            persisted["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )
        assert persisted["session_lifecycle"]["state"] == "warning"
        assert post(client, body) == result
        assert read_slot(client, issued["slot_id"])["revision"] == updated["revision"]
        # Future cooldown changes must not shorten the current window's cooldown.
        set_clock(restarted, T0 + timedelta(seconds=1400))
        cooling = read_slot(client, issued["slot_id"])["session_lifecycle"]
        # Public number-backed access never creates a new authorization merely
        # because a cooldown elapsed. Preserve the old cooldown snapshot while
        # accurately showing that its working window has expired.
        assert cooling["state"] == "expired"
        stored = restarted.state.access_mesh.store.slot("firstbyte", issued["slot_id"])
        assert stored.policy_window.rearm_at == T0 + timedelta(seconds=1560)
        assert stored.policy_window.policy.cooldown_seconds == 180
        set_clock(restarted, T0 + timedelta(seconds=1561))
        assert read_slot(client, issued["slot_id"])["session_lifecycle"]["state"] == "expired"


def test_explicit_window_extension_and_retry_leave_future_defaults_intact(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = provision(client, "uniform-window-extension")
        assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
        before = read_slot(client, issued["slot_id"])
        set_clock(app, T0 + timedelta(seconds=60))
        post(client, next_policy(before, "uniform-next-window"))
        configured = read_slot(client, issued["slot_id"])
        deadline = T0 + timedelta(seconds=1800)
        body = {
            "action": "deadline",
            "idempotency_key": "uniform-deadline-stable-retry",
            "slot_id": issued["slot_id"],
            "expected_revision": configured["revision"],
            "deadline_at": deadline.isoformat(),
        }
        result = post(client, body)
        assert post(client, body) == result
        after = read_slot(client, issued["slot_id"])
        assert after["revision"] == configured["revision"] + 1
        assert after["policy"] == configured["policy"]
        assert after["session_lifecycle"]["hard_expires_at"] == deadline.isoformat(
            timespec="microseconds"
        )
        # The authorization projection must agree with the operator projection.
        mesh = app.state.access_mesh
        slot = mesh.store.slot(mesh.store.local_node_id, issued["slot_id"])
        assert (
            mesh.store.merged_cycle(slot, mesh.clock())["hard_expires_at"]
            == after["session_lifecycle"]["hard_expires_at"]
        )


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

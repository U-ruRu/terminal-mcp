"""Independent native HTTP policy semantics; all state and credentials are synthetic."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS, internal_persistent_start

from terminal_mcp.app import create_app

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def provision(client, key):
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={
            "action": "create",
            "mode": "persistent",
            "idempotency_key": key,
            "policy": {
                "duration_seconds": 1380,
                "warning_seconds": 180,
                "draining_seconds": 90,
                "cooldown_seconds": 180,
            },
        },
    )
    assert response.status_code == 200
    receipt = response.json()
    assert receipt.get("ok") is True  # Never include a code-bearing receipt in assertion output.
    return receipt


def read_slot(client, slot_id):
    response = client.get("/actions/access/slots/" + slot_id, headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("ok") is True
    return payload["slot"]


def set_three_hours(client, slot, key):
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={
            "action": "policy",
            "idempotency_key": key,
            "slot_id": slot["slot_id"],
            "expected_revision": slot["revision"],
            "policy": {"duration_seconds": 10800},
        },
    )
    assert response.status_code == 200
    assert response.json().get("ok") is True
    return read_slot(client, slot["slot_id"])


def test_http_next_policy_does_not_change_active_window_deadline(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        app.state.access_mesh.clock = lambda: T0
        receipt = provision(client, "qa-current-window")
        assert internal_persistent_start(client, app, receipt["access_code"]).get("ok") is True
        before = read_slot(client, receipt["slot_id"])
        app.state.access_mesh.clock = lambda: T0 + timedelta(seconds=60)
        after = set_three_hours(client, before, "qa-next-policy")
        assert after["policy"]["duration_seconds"] == 10800
        assert (
            after["session_lifecycle"]["hard_expires_at"]
            == before["session_lifecycle"]["hard_expires_at"]
        )


def test_http_slot_override_keeps_other_slot_and_issuer_defaults_unchanged(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        app.state.access_mesh.clock = lambda: T0
        first = provision(client, "qa-independent-first")
        second = provision(client, "qa-independent-second")
        other_before = read_slot(client, second["slot_id"])
        defaults_before = client.get("/actions/access/defaults", headers=HEADERS).json()
        changed = set_three_hours(
            client, read_slot(client, first["slot_id"]), "qa-independent-edit"
        )
        assert changed["policy"]["duration_seconds"] == 10800
        assert read_slot(client, second["slot_id"]) == other_before
        assert client.get("/actions/access/defaults", headers=HEADERS).json() == defaults_before

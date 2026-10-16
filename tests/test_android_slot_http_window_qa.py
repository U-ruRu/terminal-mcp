"""Independent native HTTP policy semantics; all state and credentials are synthetic."""

from datetime import UTC, datetime

from test_access_mesh_operator_http import HEADERS

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

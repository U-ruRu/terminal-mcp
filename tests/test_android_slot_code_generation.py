"""Native HTTP code-generation metadata is public; actual codes stay private."""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS, internal_persistent_start
from test_android_slot_http_window_qa import T0, provision, read_slot
from test_android_slot_policy_continuation import post, set_clock

from terminal_mcp.app import create_app


def generation(payload):
    # Project the harmless scalar before asserting, so pytest never prints a code receipt.
    return payload.get("code_generation")


def test_create_start_and_policy_edits_keep_same_code_generation(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        receipt = provision(client, "uniform-generation-create")
        actual_generation = generation(receipt)
        assert actual_generation == 1
        issued = read_slot(client, receipt["slot_id"])
        assert generation(issued) == actual_generation
        assert "access_code" not in issued
        started_receipt = internal_persistent_start(client, app, receipt["access_code"])
        started_ok = started_receipt.get("ok")
        has_operator_metadata = "code_generation" in started_receipt
        assert started_ok is True
        assert not has_operator_metadata
        started = read_slot(client, receipt["slot_id"])
        assert started["revision"] > issued["revision"]
        assert generation(started) == actual_generation
        changed = post(
            client,
            {
                "action": "policy",
                "slot_id": receipt["slot_id"],
                "idempotency_key": "uniform-generation-policy",
                "expected_revision": started["revision"],
                "policy": {"duration_seconds": 10800},
            },
        )
        assert generation(changed) == actual_generation
        assert generation(read_slot(client, receipt["slot_id"])) == actual_generation


def test_rotation_generation_is_atomic_replayable_and_survives_restart(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = provision(client, "uniform-generation-restart")
        first_generation = generation(issued)
        assert first_generation == 1
        slot = read_slot(client, issued["slot_id"])
        body = {
            "action": "rotate",
            "slot_id": issued["slot_id"],
            "idempotency_key": "uniform-generation-stable-rotate",
            "expected_revision": slot["revision"],
        }
        rotated = post(client, body)
        new_generation = generation(rotated)
        assert new_generation == 2
        replay = post(client, body)
        replay_generation = generation(replay)
        replayed_same_code = rotated["access_code"] == replay["access_code"]
        assert replay_generation == new_generation
        assert replayed_same_code
        current = read_slot(client, issued["slot_id"])
        assert generation(current) == new_generation
        assert current["revision"] == slot["revision"] + 1
        listing = client.get("/actions/access/slots", headers=HEADERS).json()
        assert generation(listing["slots"][0]) == new_generation
        assert "access_code" not in listing["slots"][0]
    restored = create_app(config)
    with TestClient(restored, base_url="https://terminal.example") as client:
        set_clock(restored, T0)
        replay = post(client, body)
        replay_generation = generation(replay)
        replayed_same_code = rotated["access_code"] == replay["access_code"]
        assert replay_generation == new_generation
        assert replayed_same_code
        current = read_slot(client, issued["slot_id"])
        assert generation(current) == new_generation
        second = post(
            client,
            {
                **body,
                "idempotency_key": "uniform-generation-second-rotate",
                "expected_revision": current["revision"],
            },
        )
        second_generation = generation(second)
        assert second_generation == 3
        assert generation(read_slot(client, issued["slot_id"])) == second_generation

"""Native HTTP code-generation metadata is public; actual codes stay private."""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS
from test_android_slot_http_window_qa import T0, provision, read_slot
from test_android_slot_policy_continuation import post, set_clock

from terminal_mcp.app import create_app


def generation(payload):
    # Project the harmless scalar before asserting, so pytest never prints a code receipt.
    return payload.get("code_generation")


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

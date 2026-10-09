"""Authenticated mobile controls, strict runtime validation, durable replay and policy."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app

HEADERS = {"authorization": "Bearer mesh-test-token"}


def mutate(client, action, key, **payload):
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={"action": action, "idempotency_key": key, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_mobile_provisioning_policy_rotation_delete_and_idempotency(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        assert client.get("/actions/access/slots").status_code == 401
        first = mutate(
            client,
            "create",
            "create-legacy",
            mode="legacy",
            policy={"duration_seconds": 300, "warning_seconds": 20, "draining_seconds": 10},
        )
        assert first["ok"], first
        assert (
            mutate(
                client,
                "create",
                "create-legacy",
                mode="legacy",
                policy={"duration_seconds": 300, "warning_seconds": 20, "draining_seconds": 10},
            )
            == first
        )
        conflict = mutate(client, "create", "create-legacy", mode="persistent")
        assert conflict["code"] == "idempotency_conflict"
        persistent = mutate(client, "create", "create-persistent", mode="persistent")
        assert persistent["ok"], persistent
        view = client.get(f"/actions/access/slots/{persistent['slot_id']}", headers=HEADERS).json()
        assert view["slot"]["session_lifecycle"]["state"] == "idle"
        started = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": persistent["access_code"]},
            request_id=500,
        )
        assert started["ok"], started
        binding = {"issuer_node_id": "firstbyte", "session_number": first["access_code"]}
        attached = call(client, "executor", "session", binding)
        assert attached["ok"], attached
        rotated = mutate(
            client, "rotate", "rotate-1", slot_id=first["slot_id"], expected_revision=1
        )
        assert rotated["ok"], rotated
        assert call(client, "executor", "command_run", {"command": "true"}, request_id=501)["ok"]
        stale = mutate(client, "suspend", "stale", slot_id=first["slot_id"], expected_revision=1)
        assert stale["code"] == "revision_conflict"
        assert mutate(client, "suspend", "suspend", slot_id=first["slot_id"], expected_revision=2)[
            "ok"
        ]
        blocked = call(client, "executor", "command_run", {"command": "true"}, request_id=502)
        assert blocked["ok"] is False
        assert mutate(client, "resume", "resume", slot_id=first["slot_id"], expected_revision=3)[
            "ok"
        ]
        assert mutate(client, "delete", "delete", slot_id=first["slot_id"], expected_revision=4)[
            "ok"
        ]
        assert (
            call(client, "executor", "command_run", {"command": "true"}, request_id=503)["error"][
                "code"
            ]
            == "session_attach_required"
        )
        listed = client.get("/actions/access/slots?limit=1", headers=HEADERS).json()
        assert len(listed["slots"]) == 1 and listed["next_cursor"]
        assert "code_tag" not in str(listed) and "access_code" not in str(listed)
        next_page = client.get(
            "/actions/access/slots",
            headers=HEADERS,
            params={"limit": 1, "cursor": listed["next_cursor"]},
        ).json()
        assert (
            len(next_page["slots"]) == 1
            and next_page["slots"][0]["slot_id"] != listed["slots"][0]["slot_id"]
        )
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        assert (
            mutate(
                client,
                "create",
                "create-legacy",
                mode="legacy",
                policy={"duration_seconds": 300, "warning_seconds": 20, "draining_seconds": 10},
            )
            == first
        )
        assert (
            client.get(f"/actions/access/slots/{first['slot_id']}", headers=HEADERS).json()["slot"][
                "state"
            ]
            == "deleted"
        )


def test_defaults_persist_and_legacy_disable_does_not_disable_persistent(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        result = mutate(
            client,
            "defaults",
            "defaults1",
            expected_revision=1,
            legacy_enabled=False,
            policy={
                "duration_seconds": 600,
                "warning_seconds": 60,
                "draining_seconds": 15,
                "cooldown_seconds": 40,
            },
        )
        assert result["ok"], result
        assert mutate(client, "create", "disabled", mode="legacy")["code"] == "legacy_disabled"
        persistent = mutate(client, "create", "persistent", mode="persistent")
        assert persistent["ok"], persistent
        assert call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": persistent["access_code"]},
            request_id=4,
        )["ok"]
        malformed = mutate(
            client,
            "delete",
            "wrong-fields",
            slot_id=persistent["slot_id"],
            expected_revision=2,
            policy={"duration_seconds": 15},
        )
        assert malformed["ok"] is False
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        defaults = client.get("/actions/access/defaults", headers=HEADERS).json()
        assert defaults["revision"] == 2 and defaults["legacy_enabled"] is False
        assert defaults["policy"]["duration_seconds"] == 600
        assert (
            mutate(client, "create", "still-disabled", mode="legacy")["code"] == "legacy_disabled"
        )


def test_deadline_override_and_nonrearm_end_cooldown(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        clock = [datetime.now(UTC)]
        app.state.access_mesh.clock = lambda: clock[0]
        app.state.access_mesh.store.clock = lambda: clock[0]
        slot = mutate(
            client,
            "create",
            "fixed",
            mode="persistent",
            policy={
                "duration_seconds": 10,
                "warning_seconds": 0,
                "draining_seconds": 0,
                "cooldown_seconds": 5,
                "rearm_enabled": False,
            },
        )
        assert slot["ok"], slot
        started = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": slot["access_code"]},
            request_id=10,
        )
        assert started["ok"] and started["hard_expires_at"] is not None
        updated = mutate(
            client,
            "deadline",
            "extend",
            slot_id=slot["slot_id"],
            expected_revision=2,
            deadline_at=(clock[0] + timedelta(seconds=20)).isoformat(),
        )
        assert updated["ok"], updated
        view = client.get(f"/actions/access/slots/{slot['slot_id']}", headers=HEADERS).json()[
            "slot"
        ]
        assert view["policy"]["duration_seconds"] == 10
        assert view["session_lifecycle"]["hard_expires_at"] == (
            clock[0] + timedelta(seconds=20)
        ).isoformat(timespec="microseconds")
        ended = call(client, "access", "session", {"action": "end"}, request_id=11)
        assert ended["ok"], ended
        immediate = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": slot["access_code"]},
            request_id=12,
        )
        assert immediate["ok"], immediate
        assert immediate["hard_expires_at"] == view["session_lifecycle"]["hard_expires_at"]
        clock[0] += timedelta(seconds=5)
        restarted = call(
            client,
            "access",
            "session",
            {"action": "start", "mode": "persistent", "session_number": slot["access_code"]},
            request_id=13,
        )
        assert restarted["error"]["code"] == "session_already_started", restarted

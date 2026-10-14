"""Authenticated mobile controls, strict runtime validation, durable replay and policy."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_native_lifecycle import actor

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


def internal_persistent_start(client, app, number):
    """Mobile provisioning starts a persistent slot via internal issuer API.

    Public Access.start intentionally accepts action only.
    """

    async def start():
        return await app.state.access_mesh.issuer_session(
            actor("access"), action="start", mode="persistent", code=number
        )

    return client.portal.call(start)


def internal_persistent_end(client, app, number):
    async def end():
        return await app.state.access_mesh.issuer_session(
            actor("access"), action="end", mode="persistent", code=number
        )

    return client.portal.call(end)


def test_defaults_persist_and_legacy_disable_does_not_disable_persistent(tmp_path):
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
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
        assert internal_persistent_start(client, app, persistent["access_code"])["ok"]
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
        started = internal_persistent_start(client, app, slot["access_code"])
        assert started["ok"], started
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
        ended = internal_persistent_end(client, app, slot["access_code"])
        assert ended["ok"], ended
        immediate = internal_persistent_start(client, app, slot["access_code"])
        assert immediate["ok"], immediate
        resumed = client.get(f"/actions/access/slots/{slot['slot_id']}", headers=HEADERS).json()[
            "slot"
        ]
        assert (
            resumed["session_lifecycle"]["hard_expires_at"]
            == (view["session_lifecycle"]["hard_expires_at"])
        )
        clock[0] += timedelta(seconds=5)
        restarted = internal_persistent_start(client, app, slot["access_code"])
        assert restarted["ok"] is False and (
            restarted.get("code") == "session_already_started"
            or restarted.get("error") == "session_already_started"
        ), restarted

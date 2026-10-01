import sqlite3

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from terminal_mcp.config import Settings


def settings(tmp_path, **overrides):
    data = dict(
        database_path=tmp_path / "db.sqlite3",
        auth_database_path=tmp_path / "auth.sqlite3",
        output_cache_path=tmp_path / "output.sqlite3",
        runtime_config_path=tmp_path / "runtime.env",
        env_file_path=tmp_path / "terminal-mcp.env",
        log_path=tmp_path / "terminal-mcp.log",
        metrics_port=0,
        cwd=tmp_path,
        public_base_url="https://terminal.example",
        mcp_auth_mode="",
        actions_auth_mode="",
        persistent_agents_enabled=True,
        fleet_instance_id="",
        fleet_id="",
        fleet_signing_private_key="",
        fleet_peers_json="[]",
        fleet_v1_source_enabled=False,
        fleet_v1_authority_enabled=False,
        fleet_v1_projection_enabled=False,
        fleet_v1_public_enabled=False,
    )
    data.update(overrides)
    return Settings(**data)


def test_persistent_surface_fails_closed_when_transport_auth_is_none(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="none"))
    with TestClient(app) as client:
        response = client.post("/actions/persistent/slots/create", json={"display_name": "Alpha"})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["code"] == "persistent_auth_required"


def test_bearer_persistent_lifecycle_idempotency_and_cancel_fence(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    headers = {"Authorization": "Bearer alpha-token"}
    with TestClient(app) as client:
        created = client.post(
            "/actions/persistent/slots/create",
            json={"display_name": "Alpha"},
            headers=headers,
        ).json()
        assert created["ok"] is True
        logical_agent_id = created["slot"]["logical_agent_id"]
        selector = created["selector"]["selector"]

        play_payload = {
            "logical_agent_id": logical_agent_id,
            "expected_revision": created["slot"]["slot_revision"],
            "idempotency_key": "play-idem-0001",
        }
        played = client.post(
            "/actions/persistent/slots/play", json=play_payload, headers=headers
        ).json()
        assert played["ok"] is True
        replay = client.post(
            "/actions/persistent/slots/play", json=play_payload, headers=headers
        ).json()
        assert replay == played
        conflict = client.post(
            "/actions/persistent/slots/play",
            json={**play_payload, "expected_revision": played["slot"]["slot_revision"]},
            headers=headers,
        ).json()
        assert conflict["ok"] is False
        assert conflict["code"] == "idempotency_conflict"

        detail = client.post(
            "/actions/persistent/slots/get",
            json={"logical_agent_id": logical_agent_id},
            headers=headers,
        ).json()
        assert detail["ok"] is True
        event_types = [item["event_type"] for item in detail["audit"]]
        assert "create" in event_types
        assert "play" in event_types

        started = client.post(
            "/actions/persistent/sessions/start",
            json={
                "selector": selector,
                "expected_revision": played["slot"]["slot_revision"],
            },
            headers=headers,
        ).json()
        assert started["ok"] is True
        session = started["work_session"]
        assert session["auth_principal_id"] != "alpha-token"

        queued = client.post(
            "/actions/persistent/run",
            json={
                "cmd": "sleep 5",
                "logical_agent_id": logical_agent_id,
                "work_session_id": session["work_session_id"],
                "session_epoch": session["session_epoch"],
                "queue_id": 1,
                "task_scope": "none",
            },
            headers=headers,
        ).json()
        assert queued["ok"] is True
        cmd_hash = queued["cmd_hash"]

        generic = client.post(
            "/actions/cancel", json={"cmd_hash": cmd_hash}, headers=headers
        ).json()
        assert generic["ok"] is False
        assert generic["error"] == "cancel.persistent: exact work-session context required"

        fenced = client.post(
            "/actions/persistent/cancel",
            json={
                "cmd_hash": cmd_hash,
                "logical_agent_id": logical_agent_id,
                "work_session_id": session["work_session_id"],
                "session_epoch": session["session_epoch"],
            },
            headers=headers,
        ).json()
        assert fenced["ok"] is True

    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        principal = db.execute(
            "SELECT auth_principal_id FROM logical_agent_work_sessions WHERE logical_agent_id=?",
            (logical_agent_id,),
        ).fetchone()[0]
    assert principal != "alpha-token"
    assert "alpha-token" not in principal

def test_delete_is_idempotent_and_tombstones_selector_without_resurrection(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    headers = {"Authorization": "Bearer alpha-token"}
    with TestClient(app) as client:
        created = client.post(
            "/actions/persistent/slots/create",
            json={"display_name": "Delete me"},
            headers=headers,
        ).json()
        logical_agent_id = created["slot"]["logical_agent_id"]
        selector = created["selector"]["selector"]
        payload = {
            "logical_agent_id": logical_agent_id,
            "expected_revision": created["slot"]["slot_revision"],
            "idempotency_key": "delete-idem-0001",
        }
        deleted = client.post(
            "/actions/persistent/slots/delete", json=payload, headers=headers
        ).json()
        replay = client.post(
            "/actions/persistent/slots/delete", json=payload, headers=headers
        ).json()
        assert deleted["ok"] is True
        assert deleted["slot"]["state"] == "deleted"
        assert replay == deleted

        listed = client.post(
            "/actions/persistent/slots/list", json={}, headers=headers
        ).json()
        assert all(
            item["slot"]["logical_agent_id"] != logical_agent_id
            for item in listed["slots"]
        )

    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        row = db.execute(
            "SELECT state,deleted_at,tombstone_reason FROM logical_agents "
            "WHERE logical_agent_id=?",
            (logical_agent_id,),
        ).fetchone()
        selector_row = db.execute(
            "SELECT retired_at,tombstoned_at FROM logical_agent_selectors "
            "WHERE logical_agent_id=? AND selector=?",
            (logical_agent_id, selector),
        ).fetchone()
    assert row[0] == "deleted"
    assert row[1]
    assert row[2] == "operator_delete"
    assert selector_row[0]
    assert selector_row[1]

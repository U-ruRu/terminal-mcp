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


def test_persistent_slot_create_requires_display_name(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    with TestClient(app) as client:
        response = client.post(
            "/actions/persistent/slots/create",
            json={},
            headers={"Authorization": "Bearer alpha-token"},
        )
    assert response.status_code == 422


def test_persistent_slot_create_accepts_empty_display_name(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    with TestClient(app) as client:
        response = client.post(
            "/actions/persistent/slots/create",
            json={"display_name": ""},
            headers={"Authorization": "Bearer alpha-token"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["slot"]["display_name"] == ""


def _create_persistent_slot(client, *, name: str) -> dict:
    created = client.post(
        "/actions/persistent/slots/create",
        json={"display_name": name},
        headers={"Authorization": "Bearer alpha-token"},
    ).json()
    assert created["ok"] is True
    return created


def _delete_slot_payload(created: dict) -> dict:
    return {
        "logical_agent_id": created["slot"]["logical_agent_id"],
        "expected_revision": created["slot"]["slot_revision"],
        "idempotency_key": "delete-idem-0001",
    }


def test_persistent_slot_delete_is_idempotent(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    with TestClient(app) as client:
        created = _create_persistent_slot(client, name="Delete me")
        payload = _delete_slot_payload(created)
        first = client.post(
            "/actions/persistent/slots/delete",
            json=payload,
            headers={"Authorization": "Bearer alpha-token"},
        ).json()
        second = client.post(
            "/actions/persistent/slots/delete",
            json=payload,
            headers={"Authorization": "Bearer alpha-token"},
        ).json()
    assert first["ok"] is True
    assert second == first


def test_persistent_slot_delete_tombstones_selector(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha-token"))
    with TestClient(app) as client:
        created = _create_persistent_slot(client, name="Delete me")
        deleted = client.post(
            "/actions/persistent/slots/delete",
            json=_delete_slot_payload(created),
            headers={"Authorization": "Bearer alpha-token"},
        ).json()
        assert deleted["ok"] is True
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        slot = db.execute(
            "SELECT state,deleted_at,tombstone_reason FROM logical_agents WHERE logical_agent_id=?",
            (created["slot"]["logical_agent_id"],),
        ).fetchone()
        selector = db.execute(
            "SELECT retired_at,tombstoned_at FROM logical_agent_selectors "
            "WHERE logical_agent_id=? AND selector=?",
            (created["slot"]["logical_agent_id"], created["selector"]["selector"]),
        ).fetchone()
    assert slot[0] == "deleted" and slot[1] and slot[2] == "operator_delete"
    assert selector[0] and selector[1]

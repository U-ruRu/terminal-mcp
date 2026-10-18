"""Current operator HTTP creates slots and physically deletes their registrations."""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import settings
from test_access_mesh_operator_http import HEADERS, mutate

from terminal_mcp.app import create_app
from terminal_mcp.core.human_names import number_agent_name


def test_slot_create_assigns_a_number_without_creating_agent_identity(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        created = mutate(client, "create", "native-create", mode="persistent")
        assert created.get("ok") is True
        response = client.get("/actions/access/slots/" + created["slot_id"], headers=HEADERS)
        assert response.status_code == 200
        slot = response.json()["slot"]
        assert slot["session"] is None
        assert "logical_agent_id" not in slot
        assert "logical_agent_id" not in created
        assert slot["public_name"] == number_agent_name(created["access_code"])
        assert slot["mode"] == "persistent"
        assert slot["revision"] == 1


def test_slot_delete_removes_the_native_registration_and_number(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        created = mutate(client, "create", "native-before-delete", mode="persistent")
        assert created.get("ok") is True
        deleted = mutate(
            client,
            "delete",
            "native-delete",
            slot_id=created["slot_id"],
            expected_revision=created["revision"],
        )
        assert deleted.get("ok") is True
        response = client.get("/actions/access/slots/" + created["slot_id"], headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert response.json()["code"] == "access_mesh_slot_not_found"
        assert app.state.access_mesh.store.registry_code_slot(created["access_code"]) is None
        listed = client.get("/actions/access/slots", headers=HEADERS)
        assert listed.status_code == 200
        assert listed.json()["slots"] == []

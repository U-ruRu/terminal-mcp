"""Regression tests for the minimal public Access/attach protocol."""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.mcp.access_contracts import MeshAgentObserve, MeshAgentView


def test_observe_only_named_attached_agents_and_four_fields(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        assert call(client, "coordinator", "agent_observe", {}, meta=False) == {
            "ok": True,
            "scope": "local",
            "server_id": "firstbyte",
            "agents": [],
        }
        started = call(client, "access", "session", {"action": "start"}, request_id=401)
        assert call(
            client, "coordinator", "session", {"session_number": started["session_number"]}
        ) == {"ok": True}
        result = call(client, "coordinator", "agent_observe", {})
        assert result["ok"] and len(result["agents"]) == 1
        view = result["agents"][0]
        MeshAgentObserve.model_validate(result)
        assert set(view) == set(MeshAgentView.model_fields)
        assert result["scope"] == "local" and result["server_id"] == "firstbyte"
        assert view["public_name"] and view["last_server"] == "firstbyte"
        assert view["session_duration"] > 0 and view["last_activity"]
        assert "session_number" not in str(view)

"""Agent overview is authoritative locally and independent of message recipients."""

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app


def test_agent_observe_preserves_public_fields_but_is_explicitly_local(tmp_path):
    config = settings(tmp_path).model_copy(update={"fleet_instance_id": "server-a"})
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(client, "access", "session", {"action": "start"})
        assert call(
            client, "executor", "session", {"session_number": issued["session_number"]}
        ) == {"ok": True}

        async def unavailable_address_book():
            raise AssertionError("overview must not use active message recipients")

        app.state.access_mesh.messages.active_agent_locations = unavailable_address_book
        observed = call(client, "coordinator", "agent_observe", {})
        assert observed["scope"] == "local"
        assert observed["server_id"] == "server-a"
        assert len(observed["agents"]) == 1
        record = observed["agents"][0]
        assert {"public_name", "last_server", "session_duration", "last_activity"} <= set(record)
        assert record["last_server"] == "server-a"
        assert record["logical_agent_id"]

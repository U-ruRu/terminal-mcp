"""Agent Observe selects the server of the latest activity across Mesh nodes."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from terminal_mcp.app import create_app
from test_access_mesh_mcp_runtime import call, settings
from test_access_mesh_native_lifecycle import actor


def test_agent_observe_uses_latest_remote_activity_and_four_fields(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        started = call(client, "access", "session", {"action": "start"})
        assert started["ok"]
        assert call(client, "executor", "session", {
            "session_number": started["session_number"]
        }) == {"ok": True}
        messages = app.state.access_mesh.messages
        local = client.portal.call(messages._local_recipients)
        assert len(local) == 1
        remote = dict(local[0])
        remote["server_id"] = "bacloud"
        remote["last_active_at"] = (
            datetime.now(UTC) + timedelta(minutes=1)
        ).isoformat()

        class Peer:
            peers = (SimpleNamespace(instance_id="bacloud"),)

            async def request(self, peer, path, payload):
                assert path == "messages/recipients"
                return {"ok": True, "recipients": [remote], "after": None}

        messages.replication = Peer()
        observed = client.portal.call(
            app.state.access_mesh.observe, actor("coordinator")
        )
        assert observed["ok"] is True, observed
        assert observed["agents"] == [{
            "public_name": local[0]["public_name"],
            "last_server": "bacloud",
            "session_duration": local[0]["session_duration"]
            if "session_duration" in local[0] else
            int((datetime.fromisoformat(remote["hard_expires_at"]) -
                 datetime.fromisoformat(remote["session_started_at"])).total_seconds()),
            "last_activity": remote["last_active_at"],
        }]

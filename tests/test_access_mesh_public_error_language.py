"""Public role MCP must not leak cooldown and internal slot error vocabulary."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import SlotPolicy


def test_issuer_cooldown_is_exposed_only_as_session_expired(tmp_path):
    app = create_app(settings(tmp_path))
    mesh = app.state.access_mesh
    now = [datetime.now(UTC).replace(microsecond=0)]
    mesh.clock = mesh.store.clock = lambda: now[0]
    mesh.defaults = SlotPolicy(20, 40, True)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(client, "access", "session", {"action": "start"}, request_id=1001)
        assert issued["ok"]
        now[0] += timedelta(seconds=5)
        assert call(client, "access", "session", {"action": "end"}, request_id=1002) == {"ok": True}
        now[0] += timedelta(seconds=16)
        refused = call(client, "access", "session", {"action": "start"}, request_id=1003)
        assert refused["ok"] is False, refused
        assert refused["error"]["code"] == "session_expired", refused
        assert refused["error"]["message"] == "Сессия закончилась", refused
        assert all(
            internal not in str(refused["error"]).lower()
            for internal in ("slot", "cooldown", "window_cooldown")
        )

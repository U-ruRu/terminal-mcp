"""An expired Mesh session emits exactly one actionable local Alert."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import SlotPolicy


def test_expired_session_emits_single_message_alert_and_ack_removes_block(tmp_path):
    app = create_app(settings(tmp_path))
    mesh = app.state.access_mesh
    origin = datetime.now(UTC)
    current = [origin]
    mesh.clock = lambda: current[0]
    mesh.store.clock = mesh.clock
    mesh.defaults = SlotPolicy(20, 0, False)
    with TestClient(app, base_url="https://terminal.example") as client:
        started = call(client, "access", "session", {"action": "start"}, request_id=10)
        assert started["ok"], started
        assert call(client, "executor", "session", {
            "session_number": started["session_number"]
        }) == {"ok": True}
        current[0] += timedelta(seconds=21)
        blocked = call(client, "executor", "command_run", {
            "command": "true"
        }, request_id=11)
        assert not blocked["ok"], blocked
        assert blocked["error"]["code"] == "coordination_alert", blocked
        inbox = call(client, "executor", "message", {"action": "read"}, request_id=12)
        assert inbox["ok"], inbox
        alerts = [x for x in inbox["messages"] if x.get("mode") == "alert"]
        assert len(alerts) == 1, inbox
        alert_id = alerts[0]["message_hash"]
        unread = call(client, "executor", "command_run", {
            "command": "true"
        }, request_id=13)
        assert unread["error"]["code"] == "coordination_alert"
        wrong = call(client, "executor", "message", {
            "action": "reply", "message_hash": alert_id, "text": "Done"
        }, request_id=14)
        assert not wrong["ok"], wrong
        acknowledged = call(client, "executor", "message", {
            "action": "ack", "message_hash": alert_id
        }, request_id=15)
        assert acknowledged["ok"], acknowledged
        after = call(client, "executor", "command_run", {
            "command": "true"
        }, request_id=16)
        assert not after["ok"] and after["error"]["code"] in {
            "session_expired", "window_cooldown"
        }, after
        inbox_after = call(client, "executor", "message", {"action": "read"}, request_id=17)
        assert inbox_after["ok"]
        assert sum(1 for x in inbox_after["messages"] if x["message_hash"] == alert_id) <= 1
        with mesh.messages.store.connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM access_mesh_messages "
                "WHERE message_hash=?", (alert_id,)
            ).fetchone()[0] == 1

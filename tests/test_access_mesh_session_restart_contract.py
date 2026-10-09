"""Manual Access.start after Access.end: no implicit renewal or stale fencing."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import SlotPolicy


def test_manual_start_resumes_original_window_then_allocates_next_window(tmp_path):
    app = create_app(settings(tmp_path))
    mesh = app.state.access_mesh
    now = [datetime.now(UTC)]
    mesh.clock = lambda: now[0]
    mesh.store.clock = mesh.clock
    mesh.defaults = SlotPolicy(60, 10, True)
    with TestClient(app, base_url="https://terminal.example") as client:
        first = call(client, "access", "session", {"action": "start"}, request_id=100)
        assert first["ok"], first
        number = first["session_number"]
        slot = mesh.store.code_slot("firstbyte", number)
        original_end = slot.anchor + timedelta(seconds=60)
        assert call(client, "executor", "session", {"session_number": number}) == {"ok": True}
        now[0] += timedelta(seconds=5)
        ended = call(client, "access", "session", {"action": "end"}, request_id=101)
        assert ended == {"ok": True}, ended
        assert (
            call(client, "executor", "command_run", {"command": "true"}, request_id=102)["ok"]
            is False
        )
        now[0] += timedelta(seconds=5)
        resumed = call(client, "access", "session", {"action": "start"}, request_id=103)
        assert resumed == {"ok": True, "session_number": number}, resumed
        resumed_slot = mesh.store.code_slot("firstbyte", number)
        assert resumed_slot.anchor == slot.anchor
        assert resumed_slot.deadline_at == original_end
        assert call(client, "executor", "command_run", {"command": "true"}, request_id=104)["ok"]
        now[0] += timedelta(seconds=2)
        assert call(client, "access", "session", {"action": "end"}, request_id=105) == {"ok": True}
        # The historical policy would rearm automatically after cooldown.
        now[0] = original_end + timedelta(seconds=12)
        assert (
            call(client, "executor", "command_run", {"command": "true"}, request_id=106)["ok"]
            is False
        )
        restarted = call(client, "access", "session", {"action": "start"}, request_id=107)
        assert restarted == {"ok": True, "session_number": number}, restarted
        assert mesh.store.code_slot("firstbyte", number).anchor == now[0]
        after = call(client, "executor", "command_run", {"command": "true"}, request_id=108)
        if not after["ok"] and after["error"]["code"] == "coordination_alert":
            assert after["required_action"] == "ack"
            confirmed = call(
                client,
                "executor",
                "message",
                {"action": "ack", "message_hash": after["message_hash"]},
                request_id=109,
            )
            assert confirmed["ok"], confirmed
            after = call(client, "executor", "command_run", {"command": "true"}, request_id=110)
        assert after["ok"], after

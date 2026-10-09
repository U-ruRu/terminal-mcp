"""Expire/ACK acceptance across Mesh and pre-existing task/health gate."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import SlotPolicy


def test_expiry_alert_ack_clears_legacy_obligation_for_same_agent(tmp_path):
    """ACK must clear the expired-session Alert, even for older read gate paths."""
    app = create_app(settings(tmp_path))
    mesh = app.state.access_mesh
    now = [datetime.now(UTC)]
    mesh.clock = lambda: now[0]
    mesh.store.clock = mesh.clock
    mesh.defaults = SlotPolicy(20, 0, False)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(client, "access", "session", {"action": "start"})
        assert issued["ok"]
        assert call(client, "coordinator", "session", {"session_number": issued["session_number"]})[
            "ok"
        ]
        now[0] += timedelta(seconds=21)
        rejected = call(
            client,
            "coordinator",
            "task_manage",
            {"action": "state", "namespace": "qa", "task_id": "missing", "state": "done"},
        )
        assert rejected["error"]["code"] == "coordination_alert"
        inbox = call(client, "coordinator", "message", {"action": "read"})
        alerts = [row for row in inbox["messages"] if row.get("mode") == "alert"]
        assert len(alerts) == 1
        alert_hash = alerts[0]["message_hash"]
        ack = call(client, "coordinator", "message", {"action": "ack", "message_hash": alert_hash})
        assert ack["ok"]
        with mesh.messages.store.connect() as db:
            row = db.execute(
                "SELECT recipient_agent_id,read_at,replied_at "
                "FROM coordination_message_recipients WHERE message_hash=?",
                (alert_hash,),
            ).fetchone()
        assert row and row["read_at"] is not None, "ACK did not persist"
        pending = asyncio.run(
            mesh.messages.agent_store.message_obligations(row["recipient_agent_id"])
        )
        assert alert_hash not in {item["message_hash"] for item in pending}, (
            "An ACKed system expiry Alert is still being treated as unfulfilled"
        )

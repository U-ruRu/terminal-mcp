"""Failure injection across Mesh session issuance transaction boundaries."""

import sqlite3

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.storage.access_mesh import AccessMeshStore


def test_unknown_number_reports_business_error(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        result = call(client, "executor", "session", {"session_number": "9876"})
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_session_number"


def test_failed_number_commit_rolls_back_slot_and_retries(tmp_path, monkeypatch):
    """A crash after both inserts, before commit, cannot strand an active AccessSlot."""
    original = AccessMeshStore._after_apply
    injected = {"count": 0}

    def fail_once(self, db, event, **kwargs):
        result = original(self, db, event, **kwargs)
        if event.kind == "SlotIssued" and injected["count"] == 0:
            injected["count"] += 1
            raise RuntimeError("qa_failure_before_atomic_commit")
        return result

    monkeypatch.setattr(AccessMeshStore, "_after_apply", fail_once)
    config = settings(tmp_path)
    app = create_app(config)
    with TestClient(app, base_url="https://terminal.example") as client:
        first = call(client, "access", "session", {"action": "start"}, request_id=8181)
        assert first["ok"] is False, first
        assert injected["count"] == 1
        with sqlite3.connect(config.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM access_mesh_slot_replicas").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM access_mesh_number_claims").fetchone()[0] == 0
        second = call(client, "access", "session", {"action": "start"}, request_id=8181)
        assert second["ok"] is True, second
        assert len(second["session_number"]) == 4
        attached = call(client, "executor", "session", {"session_number": second["session_number"]})
        assert attached == {"ok": True}

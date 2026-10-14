"""Atomic executor MCP command acceptance and readback on an isolated app."""

import time

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app


def _attached_executor(client):
    number = call(client, "access", "session", {"action": "start"})["session_number"]
    assert call(client, "executor", "session", {"session_number": number})["ok"]


def test_mcp_command_run_returns_command_identifier(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        _attached_executor(client)
        result = call(client, "executor", "command_run", {"command": "printf modern-mcp"})
    assert result["ok"] is True
    assert isinstance(result["cmd_hash"], str) and result["cmd_hash"]
    assert "lines" not in result


def test_mcp_command_read_returns_completed_output(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        _attached_executor(client)
        command = call(client, "executor", "command_run", {"command": "printf modern-mcp"})
        deadline = time.monotonic() + 5
        while True:
            result = call(
                client,
                "executor",
                "command_read",
                {"cmd_hash": command["cmd_hash"]},
                meta=False,
            )
            if result["command"]["status"] in {"completed", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert result["command"]["exit_code"] == 0
    assert any("modern-mcp" in line for line in result["lines"])

"""Actual app/auth/SQLite/PowerShell wiring and durable restart on Windows."""

import time

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.host_resources import collect_host_resources


def test_three_roles_execute_and_preserve_history_after_restart(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        for role in ("access", "executor", "coordinator"):
            denied = client.post(f"/terminal-mcp/{role}/v1/mcp/")
            assert denied.status_code == 401
        number = call(client, "access", "session", {"action": "start"})["session_number"]
        for role in ("executor", "coordinator"):
            assert call(client, role, "session", {"session_number": number})["ok"]
        command = call(client, "executor", "command_run", {"command": "Write-Output 'windows-ok'"})
        command_hash = command["cmd_hash"]
        deadline = time.monotonic() + 8
        while True:
            result = call(
                client, "executor", "command_read", {"cmd_hash": command_hash}, meta=False
            )
            if result["command"]["status"] == "completed":
                break
            assert time.monotonic() < deadline, result
            time.sleep(0.02)
        assert result["command"]["exit_code"] == 0
        assert any("windows-ok" in line for line in result["lines"])
        assert call(client, "coordinator", "health", {"extended": True})["healthy"]
        assert call(client, "access", "session", {"action": "end"})["ok"]
    with TestClient(create_app(config), base_url="https://terminal.example") as client:
        result = call(client, "executor", "command_read", {"cmd_hash": command_hash}, meta=False)
        assert result["command"]["status"] == "completed"
        assert any("windows-ok" in line for line in result["lines"])
    assert collect_host_resources(tmp_path)["status"] in {"available", "partial"}

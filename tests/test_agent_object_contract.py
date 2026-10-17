"""Actual MCP transport acceptance of the universal agent object contract.

All tests use a private sqlite fixture; no production service or deployment.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import rpc, settings

from terminal_mcp.app import create_app


def test_http_invalid_command_returns_structured_validation_error(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        response = rpc(
            client,
            "executor",
            "tools/call",
            {"name": "command_run", "arguments": {"command": 2}},
        )
    assert response.status_code == 200
    payload = response.json()["result"]
    data = payload["structuredContent"]
    assert data["error"]["code"] == "input_validation_failed"
    assert data["error"]["path"] == "command"
    assert data["error"]["outcome"] == data["outcome"] == "not_committed"
    assert json.loads(payload["content"][0]["text"]) == data

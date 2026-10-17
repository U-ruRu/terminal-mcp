"""Exercise permissive inputs and structured failures through the complete MCP loop."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_compact_response_independent import _current_role_service

from terminal_mcp.mcp.roles import build_role_mcp


@pytest.mark.asyncio
async def test_missing_command_returns_a_structured_repair_over_mcp():
    server = build_role_mcp(_current_role_service(), "executor")
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("command_run", {})

    assert result.isError is False
    data = result.structuredContent
    assert data["error"]["code"] == "input_validation_failed"
    assert data["error"]["path"] == "command"
    assert json.loads(result.content[0].text) == data


@pytest.mark.asyncio
async def test_runtime_exception_stays_a_structured_result_over_mcp(monkeypatch):
    import terminal_mcp.mcp.roles as roles

    async def broken(*args, **kwargs):
        raise RuntimeError("private runtime diagnostic")

    monkeypatch.setattr(roles, "_task_list", broken)
    server = build_role_mcp(_current_role_service(), "executor")
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("task_list", {})
    assert result.isError is False
    assert result.structuredContent["error"]["code"] == "internal_error"
    assert result.structuredContent["error"]["outcome"] == "unknown"
    assert "private runtime diagnostic" not in result.model_dump_json()

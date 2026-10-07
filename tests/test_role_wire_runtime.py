"""Exercise permissive inputs and structured failures through the complete MCP loop."""

import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from mcp.shared.memory import create_connected_server_and_client_session

from terminal_mcp.mcp.roles import build_role_mcp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,cases",
    [
        (
            "executor",
            [
                ("command_run", {}, "command"),
                ("command_run", {"command": 42}, "command"),
                ("message", {"action": "reply", "text": "test"}, "message_hash"),
            ],
        ),
        (
            "coordinator",
            [
                ("task_manage", {"action": "create", "namespace": "wire-test"}, "isolation_hint"),
                (
                    "task_manage",
                    {
                        "action": "comment",
                        "namespace": "wire-test",
                        "task_id": "t",
                        "comment_text": "test",
                        "expected_revision": 1,
                    },
                    "expected_revision",
                ),
            ],
        ),
    ],
)
async def test_invalid_role_arguments_reach_runtime_over_mcp(role, cases):
    server = build_role_mcp(SimpleNamespace(persistent=None), role)
    async with create_connected_server_and_client_session(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name, arguments, path in cases:
            Draft202012Validator(tools[name].inputSchema).validate(arguments)
            result = await client.call_tool(name, arguments)
            assert result.isError is False
            data = result.structuredContent
            assert data["ok"] is False
            assert data["error"]["code"] == "input_validation_failed"
            assert data["error"]["path"] == path
            assert data["error"]["details"]["validation_errors"]
            assert json.loads(result.content[0].text) == data
            Draft202012Validator(tools[name].outputSchema).validate(data)


@pytest.mark.asyncio
async def test_runtime_exception_stays_a_structured_result_over_mcp(monkeypatch):
    import terminal_mcp.mcp.roles as roles

    async def broken(*args, **kwargs):
        raise RuntimeError("private runtime diagnostic")

    monkeypatch.setattr(roles, "_task_list", broken)
    server = build_role_mcp(SimpleNamespace(persistent=None), "executor")
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("task_list", {})
    assert result.isError is False
    assert result.structuredContent["error"]["code"] == "internal_error"
    assert result.structuredContent["error"]["outcome"] == "unknown"
    assert "private runtime diagnostic" not in result.model_dump_json()

"""The MCP adapter accepts the canonical application through its current port."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_application_boundary import Service

from terminal_mcp.adapters.actor import actor_for
from terminal_mcp.application import TerminalApplication
from terminal_mcp.mcp.server import build_mcp


@pytest.fixture
def application():
    service = Service()
    service.persistent.lifecycle = SimpleNamespace(authority_node_id="node-direct")
    return TerminalApplication(service)


def test_adapter_actor_uses_the_application_authority(application):
    assert actor_for(application, transport="mcp").node_id == "node-direct"


@pytest.mark.asyncio
async def test_application_adapter_exposes_the_current_public_tools(application):
    tools = await build_mcp(application).list_tools()
    assert [tool.name for tool in tools] == [
        "session",
        "observe",
        "message",
        "task",
        "cmd",
        "context",
        "health",
    ]


@pytest.mark.asyncio
async def test_adapter_inputs_never_expose_server_resolved_identity_fields(application):
    tools = await build_mcp(application).list_tools()
    for tool in tools:
        assert {
            "principal_id",
            "credential_id",
            "endpoint_role",
            "provider",
            "node_id",
        }.isdisjoint(tool.inputSchema.get("properties", {}))


def test_mcp_handlers_do_not_resolve_identity_or_access_storage():
    path = Path(__file__).parents[1] / "src/terminal_mcp/mcp/server.py"
    prohibited = {
        "access_identity",
        "access_message",
        "access_session_start",
        "task_store",
        "context_store",
        "persistent",
        "message_state",
        "authorize_session",
    }
    assert not [
        node.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr in prohibited
    ]

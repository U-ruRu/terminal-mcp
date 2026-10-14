import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from test_access_mesh_mcp_runtime import call, rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.mcp.schema_manifest import mesh_schema_manifest
from terminal_mcp.operation_metadata import MUTATING_ACTIONS, READ_ONLY_ACTIONS, tool_action_effects


def test_three_effective_role_contracts_match_reviewed_manifest(tmp_path):
    app = create_app(settings(tmp_path))
    servers = {
        role: getattr(app.state, f"{role}_mcp") for role in ("access", "executor", "coordinator")
    }
    actual = mesh_schema_manifest(servers)
    baseline = json.loads(
        Path("src/terminal_mcp/mcp/access_mesh_schema_baselines_knowledge_v1.json").read_text()
    )
    assert actual == baseline
    for role, data in actual["roles"].items():
        assert len(data["tools"]) == {"access": 2, "executor": 10, "coordinator": 8}[role]
        for tool in data["tools"]:
            # task_manage carries the complete action-specific strict schemas as
            # annotations. Its reviewed 40 KiB cap preserves every conditional.
            limit = 40960 if (role, tool["name"]) == ("coordinator", "task_manage") else 8192
            assert tool["planning_input_bytes"] <= limit
            assert tool["planning_output_bytes"] <= 2048
    for role, server in servers.items():
        for tool in server._tool_manager.list_tools():
            schema = tool.parameters
            Draft202012Validator.check_schema(schema)
            assert schema == {"type": "object"}
            assert tool.output_schema == {"type": "object"}
            matrix = tool_action_effects(role, tool.name, mesh=True)
            assert tool.annotations.readOnlyHint is all(
                row["readOnlyHint"] for row in matrix.values()
            )
            assert tool.annotations.destructiveHint is any(
                row["destructiveHint"] for row in matrix.values()
            )
            assert tool.annotations.idempotentHint is all(
                row["idempotentHint"] for row in matrix.values()
            )
            assert tool.annotations.openWorldHint is any(
                row["openWorldHint"] for row in matrix.values()
            )
    assert (
        servers["executor"]._tool_manager.get_tool("session").annotations.destructiveHint is False
    )
    assert servers["executor"]._tool_manager.get_tool("session").annotations.idempotentHint is False


def test_http_action_metadata_and_mesh_schemas_are_authenticated_separately(tmp_path):
    app = create_app(settings(tmp_path))
    schema = app.openapi()
    for item in schema["paths"].values():
        for operation in item.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            name = operation["operationId"]
            assert name in READ_ONLY_ACTIONS | MUTATING_ACTIONS
            assert operation["x-openai-isConsequential"] is (name in MUTATING_ACTIONS)
            assert operation["security"] == [{"BearerAuth": []}]
            assert operation["x-terminal-mcp-action-matrix"]
    for path in (
        "/actions/access/slots",
        "/actions/access/defaults",
        "/actions/access/slots/{slot_id}",
    ):
        assert schema["paths"][path]["get"]["x-openai-isConsequential"] is False
    operator = schema["paths"]["/actions/access/mutate"]["post"]
    assert "x-runtime-schema" in operator
    assert operator["x-terminal-mcp-action-matrix"]["delete"]["destructiveHint"] is True
    with TestClient(app, base_url="https://terminal.example") as client:
        assert client.get("/actions/access/slots").status_code == 401
        # Registered schemas describe side effects; bearer checks stay mandatory.
        assert rpc(client, "access", "tools/list", token=False).status_code == 401
        assert call(client, "executor", "command_read", {}, meta=False)["ok"] is True


def test_executor_and_coordinator_publish_identical_attach_and_intent_session_schema(tmp_path):
    app = create_app(settings(tmp_path))
    executor = app.state.executor_mcp._tool_manager.get_tool("session")
    coordinator = app.state.coordinator_mcp._tool_manager.get_tool("session")
    assert executor.parameters == coordinator.parameters
    schema = executor.parameters
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"session_number": "0427"})
    validator.validate({"action": "attach", "session_number": "0427"})
    validator.validate({"action": "intent", "intent": "Inspect fixture"})
    for invalid in (
        {},
        {"action": "intent"},
        {"action": "intent", "intent": "Inspect fixture", "session_number": "0427"},
        {"action": "start", "session_number": "0427"},
        {"action": "attach", "session_number": "abc"},
    ):
        # Protocol's object envelope deliberately leaves strict validation to the server.
        validator.validate(invalid)
    assert schema == {"type": "object"}
    assert "Attach this connector once" in coordinator.description
    assert "Before any domain write" in coordinator.description

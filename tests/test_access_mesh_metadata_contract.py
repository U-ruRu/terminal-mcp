import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from test_access_mesh_mcp_runtime import call, rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.mcp.schema_manifest import mesh_schema_manifest
from terminal_mcp.operation_metadata import MUTATING_ACTIONS, READ_ONLY_ACTIONS


def test_three_effective_role_contracts_match_reviewed_manifest(tmp_path):
    app = create_app(settings(tmp_path))
    servers = {
        role: getattr(app.state, f"{role}_mcp") for role in ("access", "executor", "coordinator")
    }
    actual = mesh_schema_manifest(servers)
    baseline = json.loads(
        Path("src/terminal_mcp/mcp/access_mesh_schema_baselines_v2.json").read_text()
    )
    assert actual == baseline
    for role, data in actual["roles"].items():
        assert len(data["tools"]) == {"access": 1, "executor": 10, "coordinator": 8}[role]
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
            if role in {"executor", "coordinator"} and tool.name == "session":
                # The public connector must know the Access Code is mandatory,
                # and that only attachment (never lifecycle mutation) is valid.
                assert schema["required"] == ["access_code"]
                assert schema["properties"]["action"]["const"] == "attach"
                assert schema["properties"]["access_code"]["type"] == "string"
                assert schema["properties"]["issuer_node_id"]["type"] == "string"
                assert "allOf" not in schema
            else:
                assert "required" not in schema and "allOf" not in schema
            matrix = schema["x-terminal-mcp-action-matrix"]
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
    assert servers["executor"]._tool_manager.get_tool("session").annotations.idempotentHint is True


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

def test_executor_and_coordinator_publish_identical_attach_only_session_schema(tmp_path):
    app = create_app(settings(tmp_path))
    executor = app.state.executor_mcp._tool_manager.get_tool("session")
    coordinator = app.state.coordinator_mcp._tool_manager.get_tool("session")
    assert executor.parameters == coordinator.parameters
    schema = executor.parameters
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"access_code": "0427", "issuer_node_id": "firstbyte"})
    validator.validate({"action": "attach", "access_code": "firstbyte:0427"})
    for invalid in (
        {},
        {"action": "start", "issuer_node_id": "firstbyte", "access_code": "0427"},
        {"action": "end", "issuer_node_id": "firstbyte", "access_code": "0427"},
        {"action": "detach", "issuer_node_id": "firstbyte", "access_code": "0427"},
        {"action": "attach", "issuer_node_id": "firstbyte", "access_code": "abc"},
    ):
        with pytest.raises(JsonSchemaValidationError):
            validator.validate(invalid)
    assert "Attach this connector once" in coordinator.description
    assert "mandatory" in coordinator.description

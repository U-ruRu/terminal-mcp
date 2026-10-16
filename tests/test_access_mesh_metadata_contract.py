import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from test_access_mesh_mcp_runtime import call, rpc, settings

from terminal_mcp.app import create_app
from terminal_mcp.mcp.schema_manifest import mesh_schema_manifest


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
    session = servers["executor"]._tool_manager.get_tool("session")
    Draft202012Validator.check_schema(session.parameters)
    assert session.parameters == {"type": "object"}
    assert session.output_schema == {"type": "object"}


def test_http_action_metadata_and_mesh_schemas_are_authenticated_separately(tmp_path):
    app = create_app(settings(tmp_path))
    schema = app.openapi()
    operator = schema["paths"]["/actions/access/mutate"]["post"]
    assert "x-runtime-schema" in operator
    assert operator["x-terminal-mcp-action-matrix"]["delete"]["destructiveHint"] is True
    with TestClient(app, base_url="https://terminal.example") as client:
        assert client.get("/actions/access/slots").status_code == 401
        # Registered schemas describe side effects; bearer checks stay mandatory.
        assert rpc(client, "access", "tools/list", token=False).status_code == 401
        assert call(client, "executor", "command_read", {}, meta=False)["ok"] is True

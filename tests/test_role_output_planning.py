import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from terminal_mcp.mcp.roles import build_role_mcp


def test_all_role_outputs_are_flat_and_budgeted():
    baseline = json.loads(
        (Path(__file__).parent / "fixtures/role_output_planning_bytes.json").read_text()
    )
    for role in ("executor", "coordinator"):
        server = build_role_mcp(SimpleNamespace(persistent=None), role)
        for tool in server._tool_manager.list_tools():
            schema = tool.output_schema
            Draft202012Validator.check_schema(schema)
            assert schema["type"] == "object"
            assert {"ok", "error"} <= schema["properties"].keys()
            encoded = json.dumps(schema, separators=(",", ":"))
            assert all(
                key not in encoded for key in ('"$ref"', '"$defs"', '"anyOf"', '"oneOf"', '"allOf"')
            )
            assert len(encoded.encode()) <= baseline[f"{role}.{tool.name}"]
            assert len(encoded.encode()) <= 2048
            failure = {
                "ok": False,
                "error": {
                    "code": "input_validation_failed",
                    "message": "Repair input",
                    "outcome": "not_committed",
                    "retry": "repair",
                },
            }
            Draft202012Validator(schema).validate(failure)
            tool.fn_metadata.output_model.model_validate(failure)


@pytest.mark.asyncio
async def test_planning_projection_preserves_strict_runtime_validation(monkeypatch):
    import terminal_mcp.mcp.roles as roles

    async def valid(*args, **kwargs):
        return {"ok": True, "tasks": [], "next_cursor": None}

    monkeypatch.setattr(roles, "_task_list", valid)
    server = build_role_mcp(SimpleNamespace(persistent=None), "executor")
    tool = server._tool_manager.get_tool("task_list")
    result = await tool.run({}, convert_result=True)
    assert result.isError is False
    assert result.structuredContent["ok"] is True
    Draft202012Validator(tool.output_schema).validate(result.structuredContent)
    with pytest.raises(ValidationError):
        tool.fn_metadata.output_model.model_validate({"ok": True, "tasks": "wrong type"})

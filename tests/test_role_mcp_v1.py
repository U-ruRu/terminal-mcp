import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_MODELS, planning_schema
from terminal_mcp.mcp.roles import (
    COORDINATOR_TOOLS,
    EXECUTOR_TOOLS,
    build_role_mcp,
    role_schema_contract,
)


def _service():
    return SimpleNamespace(persistent=None)


def _tool_map(role):
    return {item.name: item for item in build_role_mcp(_service(), role)._tool_manager.list_tools()}


def test_role_catalogs_are_exact_and_identity_free():
    for role, expected in (("executor", EXECUTOR_TOOLS), ("coordinator", COORDINATOR_TOOLS)):
        tools = _tool_map(role)
        assert list(tools) == list(expected)
        assert len(tools) == len(expected)
        for _name, tool in tools.items():
            schema = tool.parameters
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" not in schema
            forbidden = {
                "code",
                "sender",
                "logical_agent_id",
                "work_session_id",
                "session_epoch",
                "endpoint_role",
                "contract_version",
            }
            assert forbidden.isdisjoint(schema["properties"])
            assert "required" not in (tool.description or "").lower()


def test_planning_schema_is_typed_relaxed_runtime_superset():
    for _key, model in ROLE_TOOL_MODELS.items():
        schema = planning_schema(model)
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert "required" not in schema
        runtime = model.model_json_schema()
        assert set(runtime.get("properties", ())) == set(schema.get("properties", ()))


@pytest.mark.asyncio
async def test_structural_errors_reach_authoritative_validator():
    tools = _tool_map("executor")
    cases = (
        ("command_run", {}, "missing_required", "command"),
        ("command_cancel", {"cmd_hash": "x", "bogus": 1}, "unexpected_field", "bogus"),
        ("message", {"action": "reply", "text": "hello"}, "constraint_violation", "message_hash"),
    )
    for name, args, reason, path in cases:
        result = await tools[name].run(args, convert_result=True)
        payload = json.loads(result[0].text)
        assert payload == {
            "code": "input_validation_failed",
            "reason": reason,
            "path": path,
        }
        assert len(result[0].text.encode()) < 512


def test_application_role_authorization_sets():
    policy = CapabilityPolicy()
    executor = ActorContext(endpoint_role="executor", contract_version=1)
    coordinator = ActorContext(endpoint_role="coordinator", contract_version=1)

    assert policy.allows(executor, "commands")
    assert not policy.allows(executor, "health")
    assert policy.allows_task_action(executor, "claim")
    assert not policy.allows_task_action(executor, "create")

    assert not policy.allows(coordinator, "commands")
    assert policy.allows(coordinator, "health")
    assert policy.allows_task_action(coordinator, "create")
    assert not policy.allows_task_action(coordinator, "claim")


def test_schema_release_baseline_is_exact():
    path = Path("src/terminal_mcp/mcp/role_schema_baselines_v1.json")
    baseline = json.loads(path.read_text())
    actual = {
        "contract_version": 1,
        "roles": {
            role: role_schema_contract(role)["tools"] for role in ("executor", "coordinator")
        },
    }
    assert actual == baseline

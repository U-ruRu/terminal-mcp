"""Public MCP input budgets are finite, schema-visible, and enforced before execution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from terminal_mcp.application.input_limits import (
    MAX_COMMAND_CHARS,
    TASK_REVIEW_EVIDENCE_MAX_BYTES,
)
from terminal_mcp.application.requests import CmdRunRequest
from terminal_mcp.application.task_requests import TaskReviewRequest
from terminal_mcp.mcp.server import build_mcp


def _unbounded(schema: object, path: str = "$", *, covered: bool = False) -> list[str]:
    issues: list[str] = []
    if isinstance(schema, dict):
        covered = covered or "x-maxSerializedBytes" in schema
        kind = schema.get("type")
        if (
            not covered
            and kind == "string"
            and not any(key in schema for key in ("maxLength", "const", "enum"))
        ):
            issues.append(f"{path}:string")
        if not covered and kind == "array" and "maxItems" not in schema:
            issues.append(f"{path}:array")
        if (
            not covered
            and kind == "integer"
            and not any(key in schema for key in ("maximum", "const", "enum"))
        ):
            issues.append(f"{path}:integer")
        if (
            not covered
            and kind == "object"
            and schema.get("additionalProperties") not in (False, None)
        ):
            issues.append(f"{path}:open-object")
        for key, value in schema.items():
            if key not in {"description", "title", "default"}:
                issues.extend(_unbounded(value, f"{path}.{key}", covered=covered))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            issues.extend(_unbounded(value, f"{path}[{index}]", covered=covered))
    return issues


@pytest.mark.asyncio
async def test_all_public_mcp_input_schemas_are_finite():
    tools = await build_mcp(SimpleNamespace(persistent=None)).list_tools()
    assert {tool.name for tool in tools} == {
        "session",
        "observe",
        "message",
        "task",
        "cmd",
        "context",
        "health",
    }
    assert {tool.name: _unbounded(tool.inputSchema) for tool in tools} == {
        tool.name: [] for tool in tools
    }


def test_application_command_model_rejects_oversized_command():
    with pytest.raises(ValidationError):
        CmdRunRequest(action="run", code="1234", command="x" * (MAX_COMMAND_CHARS + 1))


def test_task_extension_json_rejects_oversized_serialized_payload():
    with pytest.raises(ValidationError, match="evidence exceeds"):
        TaskReviewRequest(
            action="review",
            code="1234",
            namespace="n",
            task_id="t",
            dimensions=["A"],
            verdict="NON_BLOCKING",
            evidence={"payload": "x" * TASK_REVIEW_EVIDENCE_MAX_BYTES},
        )


@pytest.mark.asyncio
async def test_fastmcp_rejects_oversized_message_before_application_execution():
    class ExplodingApplication:
        async def message(self, *args, **kwargs):
            raise AssertionError("application must not execute")

    service = SimpleNamespace(persistent=None, application=ExplodingApplication())
    tool = {item.name: item for item in build_mcp(service)._tool_manager.list_tools()}["message"]
    with pytest.raises(ToolError):
        await tool.run({"sender": "Agent", "text": "x" * (16 * 1024 + 1)}, convert_result=True)

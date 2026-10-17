"""Public MCP input budgets are finite, schema-visible, and enforced before execution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from terminal_mcp.application.input_limits import (
    MAX_COMMAND_CHARS,
    TASK_REVIEW_EVIDENCE_MAX_BYTES,
)
from terminal_mcp.application.requests import CmdRunRequest
from terminal_mcp.application.task_requests import TaskReviewRequest


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


def test_application_command_model_rejects_oversized_command():
    with pytest.raises(ValidationError):
        CmdRunRequest(action="run", command="x" * (MAX_COMMAND_CHARS + 1))


def test_task_extension_json_rejects_oversized_serialized_payload():
    with pytest.raises(ValidationError, match="evidence exceeds"):
        TaskReviewRequest(
            action="review",
            namespace="n",
            task_id="t",
            dimensions=["A"],
            verdict="NON_BLOCKING",
            evidence={"payload": "x" * TASK_REVIEW_EVIDENCE_MAX_BYTES},
        )

import json

import pytest
from jsonschema import Draft202012Validator

from terminal_mcp.core.public_errors import public_error
from terminal_mcp.mcp.output_contracts import (
    OutputContractViolation,
    cmd_result,
)


class FakeService:
    pass


def validate(model, result):
    Draft202012Validator(model.success_schema()).validate(result.structuredContent)
    assert json.loads(result.content[0].text)["ok"] is True


TASK = {
    "namespace": "project",
    "task_id": "T-1",
    "title": "Task",
    "lane": "implementation",
    "priority": "P1",
    "state": "in_progress",
    "operational_status": "in_progress",
    "revision": 4,
    "isolation_hint": "task/T-1",
}
TASK_LIST_ITEM = {
    "namespace": "project",
    "task_id": "T-1",
    "title": "Task",
    "lane": "implementation",
    "priority": "P1",
    "state": "in_progress",
    "operational_status": "in_progress",
    "revision": 4,
    "claimed_by": None,
    "blocking_count": 0,
    "has_checkpoint": False,
}
TASK_SNAPSHOT = {
    "namespace": "project",
    "task_id": "T-1",
    "title": "Task",
    "lane": "implementation",
    "priority": "P1",
    "state": "in_progress",
    "operational_status": "in_progress",
    "revision": 4,
    "claim": None,
    "next_action": "continue",
    "description_preview": "bounded",
    "description_truncated": False,
    "latest_checkpoint": None,
    "blocking_dependencies": [],
}


@pytest.mark.parametrize(
    "bad",
    [
        {"ok": True, "status": "queued"},
        {"ok": True, "cmd_hash": "c1", "status": "bogus"},
        {"ok": True, "cmd_hash": 42, "status": "queued"},
    ],
)
def test_contract_violation_rejects_bad_implementation_result(bad):
    with pytest.raises(OutputContractViolation, match="output_contract_violation tool=cmd"):
        cmd_result(bad, "run")


def _has_unbounded_object(node):
    if isinstance(node, dict):
        if node.get("additionalProperties") is True:
            return True
        return any(_has_unbounded_object(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_unbounded_object(value) for value in node)
    return False


def test_error_result_is_canonical_bounded_and_marked_as_error():
    secret = "raw backend detail must never reach the agent"
    raw = {
        "ok": False,
        "code": "policy_incompatible",
        "error": secret,
        "diagnostics": {"exception": secret},
    }
    result = cmd_result(raw, "run")
    expected = public_error("policy_incompatible").as_dict()
    assert result.structuredContent == expected
    assert json.loads(result.content[0].text) == expected
    assert result.isError is True
    assert secret not in result.model_dump_json()

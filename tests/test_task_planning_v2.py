"""Planning remains permissive; action fields and complete validation rules are visible."""

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from terminal_mcp.application.base import ROLE_TASK_ACTIONS
from terminal_mcp.application.task_requests import TaskRequest
from terminal_mcp.mcp.task_contract import TaskToolArguments
from terminal_mcp.mcp.task_planning import task_action_planning_schema, task_planning_schema


def test_planning_schema_is_permissive_and_carries_runtime_schema():
    plan = task_action_planning_schema()
    props = plan["properties"]
    assert {"action", "namespace", "task_id"} <= set(props)
    runtime = plan["x-runtime-schema"]
    assert runtime == TypeAdapter(TaskRequest).json_schema()
    assert not plan.get("required")
    assert not list(Draft202012Validator(plan).iter_errors({}))


def test_missing_task_request_reaches_strict_runtime_instead_of_failing_planning():
    arguments = {}
    plan = task_planning_schema(TaskToolArguments.model_json_schema())
    Draft202012Validator.check_schema(plan)
    assert not list(Draft202012Validator(plan).iter_errors(arguments))
    boundary = TaskToolArguments.model_validate(arguments)
    assert boundary.validation_error is not None
    assert list(Draft202012Validator(plan["x-runtime-schema"]).iter_errors(arguments))


def test_coordinator_flat_plan_uses_canonical_actions_without_access_code():
    plan = task_action_planning_schema(
        actions=ROLE_TASK_ACTIONS["coordinator"],
        exclude_fields={"code"},
    )
    assert "request" not in plan["properties"]
    assert "code" not in plan["properties"]
    mapping = plan["x-runtime-schema"]["discriminator"]["mapping"]
    assert set(mapping) == ROLE_TASK_ACTIONS["coordinator"]
    assert "claim" not in mapping and "release" not in mapping
    assert "state" not in plan["x-runtime-schema"]["$defs"]["TaskUpdateRequest"]["properties"]
    validator = Draft202012Validator(plan["x-runtime-schema"])
    assert list(
        validator.iter_errors(
            {
                "action": "update",
                "namespace": "test",
                "task_id": "one",
                "state": "done",
            }
        )
    )


def test_unknown_role_action_cannot_silently_expand_planning_surface():
    with pytest.raises(ValueError, match="Unknown task planning actions"):
        task_action_planning_schema(actions={"create", "invented"})

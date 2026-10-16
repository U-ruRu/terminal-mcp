"""Current HTTP router and DTO schemas, with no application/store/process composition."""

import pytest
from fastapi import FastAPI

from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.console import build_console_router
from terminal_mcp.version import __version__


@pytest.fixture(scope="module")
def schema():
    app = FastAPI(version=__version__)
    app.include_router(build_actions_router(None))
    app.include_router(build_console_router(None, None))
    return app.openapi()


def test_action_router_run_operation_id(schema):
    assert schema["paths"]["/actions/run"]["post"]["operationId"] == "runCommand"


def test_run_request_required_fields(schema):
    run_request = schema["components"]["schemas"]["RunRequest"]
    assert set(run_request["required"]) == {"agent_id", "cmd", "task_scope"}
    assert "task_scope" in schema["components"]["schemas"]["RunRequest"]["properties"]


def test_recovery_request_required_fields(schema):
    recovery_request = schema["components"]["schemas"]["RecoveryRequest"]
    assert set(recovery_request["required"]) == {"cmd"}


def test_cancel_request_required_fields(schema):
    cancel_request = schema["components"]["schemas"]["CancelRequest"]
    assert set(cancel_request["required"]) == {"cmd_hash"}


def test_read_request_has_no_required_fields(schema):
    read_request = schema["components"]["schemas"]["ReadRequest"]
    assert "required" not in read_request or read_request["required"] == []


def test_health_agent_parameter_is_optional(schema):
    health_operation = schema["paths"]["/actions/health"]["get"]
    health_params = health_operation.get("parameters", [])
    assert any(item["name"] == "agent_id" and item["required"] is False for item in health_params)


def test_agent_start_request_bounds(schema):
    start_request = schema["components"]["schemas"]["AgentStartRequest"]
    assert start_request["properties"]["task_summary"]["anyOf"][0]["maxLength"] == 120
    assert start_request["properties"]["details"]["anyOf"][0]["maxItems"] == 12
    details_schema = start_request["properties"]["details"]["anyOf"][0]
    assert details_schema["items"]["maxLength"] == 160
    assert start_request["properties"]["work_scope"]["anyOf"][0]["maxItems"] == 4


def test_coordinate_request_bounds(schema):
    coordinate_request = schema["components"]["schemas"]["CoordinateRequest"]
    assert set(coordinate_request["required"]) == {"agent_id"}
    assert coordinate_request["properties"]["intent"]["anyOf"][0]["maxLength"] == 160


def test_message_request_bounds_and_alert_destination(schema):
    message_request = schema["components"]["schemas"]["MessageRequest"]
    assert set(message_request["required"]) == {"agent_id"}
    assert message_request["properties"]["message_hash"]["anyOf"][0]["maxLength"] == 8
    assert message_request["properties"]["namespace"]["anyOf"][0]["maxLength"] == 120
    assert message_request["properties"]["task_id"]["anyOf"][0]["maxLength"] == 120
    assert "explicit destination" in message_request["properties"]["alert"]["description"]


def test_context_request_actions_and_bounds(schema):
    context_request = schema["components"]["schemas"]["ContextRequest"]
    assert context_request["required"] == ["action"]
    assert set(context_request["properties"]["action"]["enum"]) == {
        "list",
        "create",
        "update",
        "delete",
    }
    assert context_request["properties"]["summary"]["anyOf"][0]["maxLength"] == 100
    assert context_request["properties"]["id"]["anyOf"][0]["minimum"] == 1


def test_tasks_request_filters(schema):
    tasks_request = schema["components"]["schemas"]["TasksRequest"]
    assert tasks_request["properties"]["lane"]["anyOf"][0]["enum"] == [
        "implementation",
        "review",
        "release",
        "integration",
        "general",
    ]
    assert tasks_request["properties"]["state"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "qa",
        "blocked",
        "deferred",
        "done",
    ]
    assert "tags" in schema["components"]["schemas"]["TasksRequest"]["properties"]


def test_task_route_binds_the_canonical_request_schema(schema):
    operation = schema["paths"]["/actions/task"]["post"]
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/TaskRequest"


def test_task_card_current_projection_fields(schema):
    task_card = schema["components"]["schemas"]["TaskCard"]["properties"]
    assert task_card["state"]["enum"] == [
        "ready",
        "in_progress",
        "qa",
        "blocked",
        "deferred",
        "done",
    ]
    assert "isolation_hint" in task_card
    assert "blocking_dependencies" in task_card
    assert "open dependencies is blocked" in task_card["operational_status"]["description"]
    assert "ordinary claimability" in task_card["blocking_dependencies"]["description"]
    for field in (
        "archived_at",
        "archive_note",
        "owner",
        "participants",
        "claims",
        "relations",
        "comments",
    ):
        assert field in task_card


def test_task_claim_projection_fields(schema):
    claim_view = schema["components"]["schemas"]["TaskClaimView"]["properties"]
    assert {"agent_name", "claimed_at", "claim_age_seconds", "claim_intent", "role"} <= set(
        claim_view
    )


def test_managed_task_reference_fields(schema):
    managed_ref = schema["components"]["schemas"]["ManagedTaskRef"]["properties"]
    assert {"claimed_at", "claim_age_seconds", "claim_intent", "role", "isolation_hint"} <= set(
        managed_ref
    )

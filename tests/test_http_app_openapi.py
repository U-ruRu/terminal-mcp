"""Actual app composition adds operation policy metadata to its OpenAPI output."""

import pytest
from auth_http_test_helpers import settings

from terminal_mcp.app import create_app
from terminal_mcp.operation_metadata import READ_ONLY_ACTIONS
from terminal_mcp.version import __version__


@pytest.fixture
def schema(tmp_path):
    app = create_app(settings(tmp_path, auth_mode="bearer", bearer_tokens="alpha,beta"))
    return app.openapi()


def test_application_openapi_includes_current_action_routes(schema):
    expected_paths = {
        "/actions/agent/start",
        "/actions/coordinate",
        "/actions/message",
        "/actions/agents",
        "/actions/agent/finish",
        "/actions/context",
        "/actions/tasks",
        "/actions/task",
        "/actions/run",
        "/actions/read",
        "/actions/recovery",
        "/actions/cancel",
        "/actions/health",
        "/actions/console/snapshot",
    }
    assert set(schema["paths"]) == expected_paths
    assert schema["info"]["version"] == __version__


def test_application_openapi_applies_consequential_metadata(schema):
    for item in schema["paths"].values():
        for method, operation in item.items():
            if method in {"get", "post"}:
                assert operation["x-openai-isConsequential"] is (
                    operation["operationId"] not in READ_ONLY_ACTIONS
                )
